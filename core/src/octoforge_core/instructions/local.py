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
from dataclasses import dataclass

from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionLexicalSearch,
    InstructionNotFoundError,
    InstructionStore,
    InstructionType,
    InstructionVectorSearch,
    SearchHit,
    SystemInstructionError,
)
from octoforge_core.instructions.ranking import fuse, rank, rerank
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.reranker import RerankerClient

logger = logging.getLogger(__name__)

EMBEDDED_TEXT_SEPARATOR = "\n"
DEFAULT_RERANK_CANDIDATES = 20
# Stand-in for an unconfigured model name. It is a value the store compares
# against, never a claim about what produced anything: records stamped with it
# stay stamped, and the moment a real model name is configured they all read as
# stale and get re-embedded, which is the correct outcome.
UNKNOWN_EMBEDDING_MODEL = "unknown"
# How many records one startup sweep re-embeds. A model change makes the whole
# table stale at once, and embedding it could take minutes; the sweep takes a
# slice per boot instead, so startup stays fast and the table converges.
DEFAULT_RESYNC_BATCH = 256
SYSTEM_RECORD_MESSAGE = (
    "'{title}' is a system instruction managed by the registry; it cannot be modified"
)


@dataclass(frozen=True, slots=True)
class InstructionSearchPolicy:
    """Tuning of the instructions facade, bundled to keep one readable constructor.

    `embedding_model` is the identity stamped on every vector this service
    writes. It is configuration rather than something read off the embedder:
    only the composition root knows which backend was wired, and the
    `EmbeddingClient` port stays a single `embed` method.
    """

    rerank_candidates: int = DEFAULT_RERANK_CANDIDATES
    embedding_model: str = UNKNOWN_EMBEDDING_MODEL
    resync_batch: int = DEFAULT_RESYNC_BATCH


# A frozen instance, so the default is shared rather than rebuilt per call and
# ruff's function-call-in-default rule stays satisfied.
DEFAULT_SEARCH_POLICY = InstructionSearchPolicy()


class LocalInstructionService:
    """InstructionService over an injected store and an embedder."""

    def __init__(
        self,
        store: InstructionStore,
        embedder: EmbeddingClient,
        reranker: RerankerClient | None = None,
        policy: InstructionSearchPolicy = DEFAULT_SEARCH_POLICY,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker
        self._rerank_candidates = policy.rerank_candidates
        self._embedding_model = policy.embedding_model
        self._resync_batch = policy.resync_batch
        # usage counters are written off the answer's path; the tasks are held
        # so the loop cannot collect one mid-write
        self._pending_bumps: set[asyncio.Task[None]] = set()

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
        kinds = _wanted_kinds(kind, exclude)
        (query_embedding,) = await self._embedder.embed((query,))
        shortlist = await self._shortlist(query, query_embedding, fetch, user_id, kinds)
        hits = await self._apply_reranker(query, shortlist, len(shortlist) if mixed else k)
        if mixed:
            hits = _cap_types(hits, k)
        self._bump_usage_later(tuple(hit.instruction.id for hit in hits))
        return hits

    def _bump_usage_later(self, instruction_ids: tuple[str, ...]) -> None:
        """Count the hits without making the caller wait for the write.

        `usage_count` is a statistic, not part of the answer: nothing reads it
        before the next search, and a lost increment costs nothing. Awaiting
        it put a round trip between the agent and the records it had already
        been given — free beside the database, 36 ms across a tunnel to one.

        The task is kept referenced until it finishes, or the event loop is
        free to garbage-collect it mid-flight.
        """
        if not instruction_ids:
            return
        task = asyncio.create_task(self._bump_usage_quietly(instruction_ids))
        self._pending_bumps.add(task)
        task.add_done_callback(self._pending_bumps.discard)

    async def _bump_usage_quietly(self, instruction_ids: tuple[str, ...]) -> None:
        """Write the counters; a failure must not surface anywhere."""
        try:
            await self._store.bump_usage(instruction_ids)
        except Exception:
            logger.warning("usage counters not recorded", exc_info=True)

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
        # one lookup, two questions: whether a system record blocks the write
        # and whether this caller authored the published one. Asking twice cost
        # an extra round trip on every save.
        published = await self._store.get_by_title(title, kind)
        if published is not None and published.system:
            raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=title))
        author_edit = (
            published is not None and not published.system and published.author_id == user_id
        )
        embedding = await self._embed_lenient(title, content)
        draft = InstructionDraft(
            kind=kind,
            title=title,
            content=content,
            tags=tags,
            embedding=embedding,
            # no vector means no model claim: the record reads as stale and the
            # startup sweep finishes what the failed backend could not
            embedding_model=self._embedding_model if embedding else None,
            system=False,
            owner_id=None if author_edit else user_id,
            author_id=user_id,
        )
        return await self._store.upsert(draft)

    async def save_public(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Upsert a public non-system record (integration syncs, e.g. MCP mirrors).

        An unchanged record is returned as-is — re-embedding identical
        content on every sweep is a wasted (paid with an HTTP backend) call.
        A system record holding the (kind, title) slot refuses the write:
        the startup registry sync owns it.
        """
        existing = await self._store.get_by_title(title, kind)
        if existing is not None:
            if existing.system:
                raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=title))
            if existing.owner_id is None and existing.content == content and existing.tags == tags:
                return existing
        embedding = await self._embed_lenient(title, content)
        draft = InstructionDraft(
            kind=kind,
            title=title,
            content=content,
            tags=tags,
            embedding=embedding,
            # no vector means no model claim: the record reads as stale and the
            # startup sweep finishes what the failed backend could not
            embedding_model=self._embedding_model if embedding else None,
            system=False,
            owner_id=None,
        )
        return await self._store.upsert(draft)

    async def list_public_by_prefix(self, kind: InstructionType, prefix: str) -> list[Instruction]:
        """Public records of the kind titled under `prefix` (a sync's namespace)."""
        return await self._store.list_public_by_prefix(kind, prefix)

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
            embedding_model=self._embedding_model,
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

    async def resync_embeddings(self) -> int:
        """Re-embed records the configured model did not produce; return the count.

        Batch-embeds one bounded slice per call; individual `set_embedding`
        misses (a record deleted mid-sweep) are simply skipped. The bound is
        what keeps a model change from turning the next startup into a
        full-table job before the process can serve anyone.
        """
        pending = await self._store.list_stale_embeddings(self._embedding_model, self._resync_batch)
        if not pending:
            return 0
        embeddings = await self._embedder.embed(
            tuple(_embedded_text(record.title, record.content) for record in pending)
        )
        stored = 0
        for record, embedding in zip(pending, embeddings, strict=True):
            if await self._store.set_embedding(record.id, embedding, self._embedding_model):
                stored += 1
        return stored

    async def _embed(self, title: str, content: str) -> tuple[float, ...]:
        (embedding,) = await self._embedder.embed((_embedded_text(title, content),))
        return embedding

    async def _embed_lenient(self, title: str, content: str) -> tuple[float, ...]:
        """Embed for an agent-facing save; a backend failure defers, not loses.

        The fact the user just asked to remember must not vanish because the
        embedding backend is down: the record is stored with an empty vector
        (found by exact title until then) and the startup `resync_embeddings`
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

    def _shortlist_size(self, k: int) -> int:
        return max(k, self._rerank_candidates) if self._reranker is not None else k

    async def _shortlist(
        self,
        query: str,
        query_embedding: tuple[float, ...],
        fetch: int,
        user_id: str | None,
        kinds: tuple[InstructionType, ...],
    ) -> list[SearchHit]:
        """Produce the scored shortlist, hybrid where the store can, cosine otherwise.

        Two retrievers answer different questions. The vector search finds
        records *about* the query; BM25 finds records that literally say it,
        which is the only thing that works for a product name, an error code or
        a rare acronym. Where both exist their orderings are fused; where only
        one does, nothing about the result shape changes.
        """
        # the two retrievers do not depend on each other, and waiting for one
        # before starting the other costs a full round trip to the database —
        # nothing beside it, a tenth of a second across a tunnel to one
        vector_hits, lexical_hits = await asyncio.gather(
            self._vector_candidates(query_embedding, fetch, user_id, kinds),
            self._lexical_candidates(query, fetch, user_id, kinds),
        )
        if lexical_hits is None:
            # off the event loop: brute-force scoring is CPU work that grows
            # with the table and must not stall every other dialog
            return await asyncio.to_thread(rank, vector_hits, query, query_embedding, fetch)
        # fusion reads positions, not vectors: cheap enough to stay inline
        return fuse([vector_hits, lexical_hits], query, fetch)

    async def _vector_candidates(
        self,
        query_embedding: tuple[float, ...],
        fetch: int,
        user_id: str | None,
        kinds: tuple[InstructionType, ...],
    ) -> list[EmbeddedInstruction]:
        """Fetch the ranking input: vector search on the store side when supported."""
        if isinstance(self._store, InstructionVectorSearch):
            return await self._store.search_by_vector(query_embedding, fetch, user_id, kinds)
        rows = await self._store.list_with_embeddings(user_id)
        return [row for row in rows if not kinds or row.instruction.type in kinds]

    async def _lexical_candidates(
        self,
        query: str,
        fetch: int,
        user_id: str | None,
        kinds: tuple[InstructionType, ...],
    ) -> list[EmbeddedInstruction] | None:
        """BM25 candidates, or None when the store has no lexical capability.

        None rather than an empty list on purpose: "this database cannot do
        lexical search" and "nothing matched the words" call for different
        behaviour upstream, and conflating them would let an unmatched query
        silently halve the ranking evidence.
        """
        if not isinstance(self._store, InstructionLexicalSearch):
            return None
        return await self._store.search_by_text(query, fetch, user_id, kinds)

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


def _wanted_kinds(
    kind: InstructionType | None,
    exclude: tuple[InstructionType, ...],
) -> tuple[InstructionType, ...]:
    """Turn "this kind" / "all but these" into the explicit list the store takes.

    The store needs the positive form because it applies the predicate inside
    the query: filtering after a top-N would spend the whole budget on types
    the caller did not ask for and hand back nothing.
    """
    if kind is not None:
        return (kind,)
    return tuple(candidate for candidate in InstructionType if candidate not in exclude)
