#!/usr/bin/env python3
"""Write a working `.env` for a fresh clone without external dependencies."""

import importlib
import os
import sys
from pathlib import Path

_TOOLS_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, _TOOLS_DIR)
try:
    _cli = importlib.import_module("quickstart_cli")
    _crypto = importlib.import_module("quickstart_crypto")
    _env = importlib.import_module("quickstart_env")
    _text = importlib.import_module("quickstart_text")
finally:
    sys.path.remove(_TOOLS_DIR)

parser = _cli.parser
fernet_key = _crypto.fernet_key
hash_password = _crypto.hash_password
EnvValues = _env.EnvValues
missing_requirements = _env.missing_requirements
read_values = _env.read_values
render_env = _env.render_env
ADMIN_USERNAME = _text.ADMIN_USERNAME
DEFAULT_LLM_BASE_URL = _text.DEFAULT_LLM_BASE_URL
DEFAULT_LLM_MODEL = _text.DEFAULT_LLM_MODEL
DONE_MESSAGE = _text.DONE_MESSAGE
ENV_PATH = _text.ENV_PATH
EXISTING_GAPS_MESSAGE = _text.EXISTING_GAPS_MESSAGE
EXISTING_OK_MESSAGE = _text.EXISTING_OK_MESSAGE
GAP_HINT = _text.GAP_HINT
MISSING_KEY_MESSAGE = _text.MISSING_KEY_MESSAGE
PLACEHOLDER_KEY = _text.PLACEHOLDER_KEY

__all__ = [
    "PLACEHOLDER_KEY",
    "fernet_key",
    "hash_password",
    "main",
    "missing_requirements",
    "read_values",
]


def ask(prompt: str, default: str) -> str:
    if not sys.stdin.isatty():
        return default
    return input(f"{prompt} [{default}]: ").strip() or default


def resolve_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    from_env = os.environ.get("OF_LLM_API_KEY", "").strip()
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        return PLACEHOLDER_KEY
    return (
        input(
            f"LLM API key (any OpenAI-compatible endpoint) [{PLACEHOLDER_KEY}]: "
        ).strip()
        or PLACEHOLDER_KEY
    )


def existing_env(path: Path) -> int:
    gaps = missing_requirements(read_values(path.read_text(encoding="utf-8")))
    if not gaps:
        print(EXISTING_OK_MESSAGE.format(path=path))
        return 0
    print(
        EXISTING_GAPS_MESSAGE.format(path=path, gaps="\n".join(gaps)), file=sys.stderr
    )
    print(GAP_HINT.format(path=path), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parser(ENV_PATH).parse_args(argv)
    path = Path(args.env)
    if path.exists():
        return existing_env(path)
    values = EnvValues(
        args.llm_base_url or ask("LLM base URL", DEFAULT_LLM_BASE_URL),
        resolve_key(args.llm_key),
        args.llm_model or ask("LLM model", DEFAULT_LLM_MODEL),
        args.embedding_model,
    )
    content, shown_password = render_env(values)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    print(
        DONE_MESSAGE.format(path=path, username=ADMIN_USERNAME, password=shown_password)
    )
    if values.llm_api_key == PLACEHOLDER_KEY:
        print(
            MISSING_KEY_MESSAGE.format(path=path, placeholder=PLACEHOLDER_KEY),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
