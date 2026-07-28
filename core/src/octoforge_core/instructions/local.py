"""Local in-process implementation of the InstructionService facade.

The storage layer is the `InstructionStore` port injected through the
constructor: the shipped `SqlAlchemyInstructionStore` ranks brute-force in
the process, an installer can substitute a pgvector/vector-DB store
(implementing `InstructionVectorSearch`) without touching this service.
When a reranker is configured, the cosine shortlist is re-scored by a
cross-encoder (the b2e two-stage pattern); a reranker failure degrades
gracefully to the cosine shortlist.

Visibility: agent-facing search sees public records plus the caller's own
private ones; new records are always private to their author, and only the
admin-facing `publish` makes a record public.
"""

import asyncio
import logging

from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionNotFoundError,
    InstructionStore,
    InstructionType,
    InstructionVectorSearch,
    SearchHit,
    SystemInstructionError,
)
from octoforge_core.instructions.ranking import rank, rerank
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.reranker import RerankerClient

logger = logging.getLogger(__name__)

EMBEDDED_TEXT_SEPARATOR = "\n"
DEFAULT_RERANK_CANDIDATES = 20
SYSTEM_RECORD_MESSAGE = (
    "'{title}' is a system instruction managed by the registry; it cannot be modified"
)


class LocalInstructionService:
    """InstructionService over an injected store and an embedder."""

    def __init__(
        self,
        store: InstructionStore,
        embedder: EmbeddingClient,
        reranker: RerankerClient | None = None,
        rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates

    async def search(
        self,
        user_id: str,
        query: str,
        k: int,
        kind: InstructionType | None = None,
    ) -> list[SearchHit]:
        """Rank the records visible to `user_id`; `kind` filters before ranking.

        Endpoint records are excluded unless `kind` names them explicitly:
        skills reference endpoints by name and `endpoint_get` resolves the
        contract at call time (late binding), so mixing thousands of endpoint
        records into every planning search would only drown the skills and
        knowledge the agent is actually orienting by.
        """
        return await self._search(user_id, query, k, kind, exclude=(InstructionType.ENDPOINT,))

    async def search_all(
        self,
        query: str,
        k: int,
        kind: InstructionType | None = None,
    ) -> list[SearchHit]:
        """Rank the whole table with no visibility filter (admin surface).

        Memory records only surface when `kind` names them explicitly: a
        cross-user search must not leak personal memories into an admin's
        general instruction lookup.
        """
        return await self._search(None, query, k, kind, exclude=(InstructionType.MEMORY,))

    async def _search(
        self,
        user_id: str | None,
        query: str,
        k: int,
        kind: InstructionType | None,
        exclude: tuple[InstructionType, ...] = (),
    ) -> list[SearchHit]:
        """Embed the query, rank the candidates by cosine and bump usage of the hits.

        Candidates come from the store: vector-capable stores run the search
        on their side, the rest hand over the visible rows for brute-force
        cosine. With a reranker configured, the cosine stage returns a
        shortlist of `rerank_candidates` which the cross-encoder re-scores.
        A mixed (kind-less) search additionally caps every type's share of
        the top-k, so one dominant record type cannot push the others out —
        the shortlist is oversampled to have something to backfill with.
        `exclude` only applies to kind-less searches: an explicit kind wins.
        """
        if not query.strip() or k <= 0:
            return []
        mixed = kind is None
        # a mixed search oversamples the cosine stage so the type caps have a
        # tail to backfill from; with a reranker the shortlist stays at
        # rerank_candidates — the cross-encoder's input size is its cost knob
        fetch = self._shortlist_size(k)
        if mixed and self._reranker is None:
            fetch = max(fetch, k * 3)
        (query_embedding,) = await self._embedder.embed((query,))
        candidates = await self._candidates(query_embedding, fetch, user_id)
        if kind is not None:
            candidates = [
                candidate for candidate in candidates if candidate.instruction.type is kind
            ]
        else:
            candidates = [
                candidate for candidate in candidates if candidate.instruction.type not in exclude
            ]
        # off the event loop: brute-force scoring is CPU work that grows with
        # the table and must not stall every other dialog in the process
        shortlist = await asyncio.to_thread(rank, candidates, query, query_embedding, fetch)
        hits = await self._apply_reranker(query, shortlist, len(shortlist) if mixed else k)
        if mixed:
            hits = _cap_types(hits, k)
        await self._store.bump_usage(tuple(hit.instruction.id for hit in hits))
        return hits

    async def save(
        self,
        user_id: str,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Upsert the caller's record (version bump on replace).

        Agent-facing: refuses to overwrite a system (registry-owned) record.
        A public record with the same title stays untouched for everyone —
        the save creates the caller's private copy — with one exception:
        the record's AUTHOR. Publication moves visibility, not authorship,
        so the author's save updates their published record in place (it
        stays public, they stay its author).
        """
        await self._ensure_not_system(kind, title)
        published = await self._store.get_by_title(title, kind)
        author_edit = (
            published is not None and not published.system and published.author_id == user_id
        )
        draft = InstructionDraft(
            kind=kind,
            title=title,
            content=content,
            tags=tags,
            embedding=await self._embed_lenient(title, content),
            system=False,
            owner_id=None if author_edit else user_id,
            author_id=user_id,
        )
        return await self._store.upsert(draft)

    async def save_system(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Upsert a system record; a public (kind, title) match is adopted as system.

        An unchanged system record is returned as-is: re-embedding identical
        title+content on every startup registry sync is a wasted (paid with
        an HTTP backend) call. Private records with the same title are not
        touched — the registry only ever adopts public rows.
        """
        existing = await self._store.get_by_title(title, kind)
        if (
            existing is not None
            and existing.system
            and existing.content == content
            and existing.tags == tags
        ):
            return existing
        draft = InstructionDraft(
            kind=kind,
            title=title,
            content=content,
            tags=tags,
            embedding=await self._embed(title, content),
            system=True,
            owner_id=None,
        )
        return await self._store.upsert(draft)

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None = None,
        user_id: str | None = None,
    ) -> Instruction:
        """Return the record by title: the caller's own copy first, then public."""
        instruction = None
        if user_id is not None:
            instruction = await self._store.get_by_title(name, kind, owner_id=user_id)
        if instruction is None:
            instruction = await self._store.get_by_title(name, kind)
        if instruction is None:
            raise InstructionNotFoundError(name)
        return instruction

    async def list_system(self) -> list[Instruction]:
        """Return every system (registry-owned) record."""
        return await self._store.list_system()

    async def delete(self, user_id: str, instruction_id: str) -> None:
        """Delete the caller's own record by id; anything else is a NotFound."""
        if not await self._store.delete_by_id(instruction_id, user_id):
            raise InstructionNotFoundError(instruction_id)

    async def publish(self, instruction_id: str) -> Instruction:
        """Make the record public; admin surface, not an agent tool."""
        instruction = await self._store.publish(instruction_id)
        if instruction is None:
            raise InstructionNotFoundError(instruction_id)
        return instruction

    async def delete_public(self, instruction_id: str) -> None:
        """Delete a public non-system record by id; admin surface, not an agent tool.

        A private id answers NotFound (private deletion stays owner-scoped);
        a system record refuses — the startup registry sync owns it and would
        recreate it on the next boot anyway.
        """
        record = await self._store.get(instruction_id)
        if record is None or record.owner_id is not None:
            raise InstructionNotFoundError(instruction_id)
        if record.system:
            raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=record.title))
        # the public (kind, title) pair is unique, so this hits exactly our record
        if not await self._store.delete_by_title(record.title, record.type):
            raise InstructionNotFoundError(instruction_id)

    async def delete_system(self, name: str, kind: InstructionType) -> None:
        """Delete a public/system record regardless of the flag (registry sync only)."""
        if not await self._store.delete_by_title(name, kind):
            raise InstructionNotFoundError(name)

    async def reembed_missing(self) -> int:
        """Embed and store vectors for records saved without one; return the count.

        Batch-embeds everything in one call; individual `set_embedding` misses
        (a record deleted mid-sweep) are simply skipped.
        """
        pending = await self._store.list_missing_embeddings()
        if not pending:
            return 0
        embeddings = await self._embedder.embed(
            tuple(_embedded_text(record.title, record.content) for record in pending)
        )
        stored = 0
        for record, embedding in zip(pending, embeddings, strict=True):
            if await self._store.set_embedding(record.id, embedding):
                stored += 1
        return stored

    async def _embed(self, title: str, content: str) -> tuple[float, ...]:
        (embedding,) = await self._embedder.embed((_embedded_text(title, content),))
        return embedding

    async def _embed_lenient(self, title: str, content: str) -> tuple[float, ...]:
        """Embed for an agent-facing save; a backend failure defers, not loses.

        The fact the user just asked to remember must not vanish because the
        embedding backend is down: the record is stored with an empty vector
        (found by exact title until then) and the startup `reembed_missing`
        sweep finishes the job.
        """
        try:
            return await self._embed(title, content)
        except Exception:
            logger.warning(
                "embedding failed, saving %r without a vector (reembed sweep will fix it)",
                title,
                exc_info=True,
            )
            return ()

    async def _ensure_not_system(self, kind: InstructionType, title: str) -> None:
        existing = await self._store.get_by_title(title, kind)
        if existing is not None and existing.system:
            raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=title))

    def _shortlist_size(self, k: int) -> int:
        return max(k, self._rerank_candidates) if self._reranker is not None else k

    async def _candidates(
        self,
        query_embedding: tuple[float, ...],
        fetch: int,
        user_id: str | None,
    ) -> list[EmbeddedInstruction]:
        """Fetch the ranking input: vector search on the store side when supported."""
        if isinstance(self._store, InstructionVectorSearch):
            return await self._store.search_by_vector(query_embedding, fetch, user_id)
        return await self._store.list_with_embeddings(user_id)

    async def _apply_reranker(
        self,
        query: str,
        shortlist: list[SearchHit],
        k: int,
    ) -> list[SearchHit]:
        if self._reranker is None or not shortlist:
            return shortlist
        pairs = tuple(
            (query, _embedded_text(hit.instruction.title, hit.instruction.content))
            for hit in shortlist
        )
        try:
            scores = await self._reranker.score(pairs)
        except Exception:
            # The reranker is an optional second stage: an outage must not take
            # down search — degrade to the cosine shortlist.
            logger.warning("reranker failed, falling back to the cosine shortlist", exc_info=True)
            return shortlist[:k]
        return rerank(shortlist, scores, k, query)


def _cap_types(hits: list[SearchHit], k: int) -> list[SearchHit]:
    """Trim a mixed ranking to k hits with no type crowding the others out.

    Too many records of one kind confuse the model more than they help
    (agreed 2026-07-25): the first pass keeps every type's share at
    ceil(k/2) so other types backfill from the oversampled tail; the second
    pass relaxes the cap when there is nothing else to show — a cap must
    diversify, never starve the result.
    """
    cap = max(1, -(-k // 2))  # ceil(k/2) without importing math
    taken: list[SearchHit] = []
    skipped: list[SearchHit] = []
    per_type: dict[InstructionType, int] = {}
    for hit in hits:
        if len(taken) == k:
            return taken
        kind = hit.instruction.type
        if per_type.get(kind, 0) == cap:
            skipped.append(hit)
            continue
        per_type[kind] = per_type.get(kind, 0) + 1
        taken.append(hit)
    filled = taken + skipped[: k - len(taken)]
    filled.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    return filled


def _embedded_text(title: str, content: str) -> str:
    return f"{title}{EMBEDDED_TEXT_SEPARATOR}{content}"
