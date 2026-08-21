"""Register every ORM model package on Base.metadata."""

from importlib import import_module

MODEL_MODULES = (
    "octoforge_core.context.models",
    "octoforge_core.cron.models",
    "octoforge_core.datasets.models",
    "octoforge_core.dialogs.models",
    "octoforge_core.identity.models",
    "octoforge_core.instructions.models",
    "octoforge_core.mcp.models",
    "octoforge_core.net.collections.models",
    "octoforge_core.params.models",
    "octoforge_core.secrets.models",
    "octoforge_core.settings.models",
    "octoforge_core.tariffs.models",
    "octoforge_core.tasks.models",
)


def register_models() -> None:
    """Import model modules for their declarative table registrations."""
    for module in MODEL_MODULES:
        import_module(module)
