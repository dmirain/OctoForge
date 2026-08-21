"""Knowledge, data and outbound-call service composition."""

from octoforge_core.composition_types import InstructionServiceOptions
from octoforge_core.datasets.api import DatasetService, DatasetStore
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.instructions.api import InstructionService, InstructionStore
from octoforge_core.instructions.local import (
    InstructionSearchOptions,
    InstructionSearchPolicy,
    LocalInstructionService,
)
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.net.external import (
    ExternalCallConfig,
    ExternalCallExecutor,
    ExternalCallServices,
)
from octoforge_core.tariffs.api import LimitGate

DEFAULT_INSTRUCTION_OPTIONS = InstructionServiceOptions()


def build_instruction_service(
    store: InstructionStore,
    embedder: EmbeddingClient,
    options: InstructionServiceOptions = DEFAULT_INSTRUCTION_OPTIONS,
) -> InstructionService:
    """Build the instructions facade with explicit search policy and model identity."""
    return LocalInstructionService(
        store,
        embedder,
        InstructionSearchOptions(
            reranker=options.reranker,
            policy=InstructionSearchPolicy(
                rerank_candidates=options.rerank_candidates,
                embedding_model=options.embedding_model,
            ),
        ),
    )


def build_dataset_service(
    store: DatasetStore,
    embedder: EmbeddingClient,
    limits: LimitGate | None = None,
) -> DatasetService:
    """Build the datasets facade over the given store and embedder."""
    return LocalDatasetService(store, embedder, limits=limits)


def build_external_executor(
    services: ExternalCallServices,
    config: ExternalCallConfig | None = None,
) -> ExternalCallExecutor:
    """Build the executor of stored HTTP and delegated endpoint contracts."""
    return ExternalCallExecutor(services, config or ExternalCallConfig())
