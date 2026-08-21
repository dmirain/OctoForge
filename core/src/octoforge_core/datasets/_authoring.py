"""Dataset creation policy: quotas, embedding text and persistence."""

from octoforge_core.datasets.requests import DatasetDefinition
from octoforge_core.datasets.store_ports import DatasetStore
from octoforge_core.datasets.types import Dataset, DatasetQuotaError
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.tariffs.api import LimitGate

EMBEDDED_TEXT_SEPARATOR = "\n"


class DatasetAuthor:
    """Create embedded descriptors while enforcing the owner's plan cap."""

    def __init__(
        self,
        store: DatasetStore,
        embedder: EmbeddingClient,
        limits: LimitGate | None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._limits = limits

    async def create(self, definition: DatasetDefinition) -> Dataset:
        await self._check_quota(definition.owner_user_id)
        text = EMBEDDED_TEXT_SEPARATOR.join(
            (definition.name, definition.description, definition.usage_notes)
        )
        (embedding,) = await self._embedder.embed((text,))
        return await self._store.create(definition, embedding)

    async def _check_quota(self, owner_user_id: str) -> None:
        if self._limits is None:
            return
        cap = await self._limits.max_datasets(owner_user_id)
        if cap is None:
            return
        existing = len(await self._store.list_with_embeddings(owner_user_id))
        if existing >= cap:
            raise DatasetQuotaError(cap)
