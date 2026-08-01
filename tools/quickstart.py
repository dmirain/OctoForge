#!/usr/bin/env python3
"""Write a working `.env` so a fresh clone can be started in one command.

    python3 tools/quickstart.py                   # ask for the LLM key (or take it from OF_LLM_API_KEY)
    python3 tools/quickstart.py --llm-key sk-...  # non-interactive

Three values are impossible to guess and used to be three manual steps before
anything answered at all: the operator credential (an empty
`OF_ADMIN_PASSWORD_HASH` makes the whole HTTP surface answer 503 — it fails
closed on purpose), the secret-store master key, and the LLM endpoint. This
generates the first two, asks for the third and prints the credential once.

Stdlib only, by design: it has to run before `make install` and outside the
virtualenv, from the docker path where no Python dependency of the project is
installed at all. The password hashing below therefore duplicates
`octoforge_server.auth.hash_password`; `web/tests/test_quickstart.py` verifies the
two still agree.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path

ENV_PATH = Path(".env")
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
PLACEHOLDER_KEY = "REPLACE_ME"
ADMIN_USERNAME = "admin"

# Keep in sync with octoforge_server.auth (same constants, same format).
HASH_SCHEME = "pbkdf2_sha256"
HASH_ITERATIONS = 240_000
HASH_SEPARATOR = ":"
SALT_BYTES = 16
PASSWORD_BYTES = 18  # 24 characters of base64url
FERNET_KEY_BYTES = 32

ENV_TEMPLATE = """\
# Written by tools/quickstart.py. Every setting and its comment lives in
# .env.example; this file holds only what a first run needs.

# The OpenAI-compatible LLM endpoint. Embeddings inherit this URL and key
# unless OF_EMBEDDING_* says otherwise, so recall works with one credential.
OF_LLM_BASE_URL={llm_base_url}
OF_LLM_API_KEY={llm_api_key}
OF_LLM_MODEL={llm_model}
OF_EMBEDDING_MODEL={embedding_model}

# Operator credential for the chat UI, the API and the console at /admin.html.
# The password itself was printed once by the script and is not stored.
OF_ADMIN_USERNAME={admin_username}
OF_ADMIN_PASSWORD_HASH={admin_password_hash}

# Master key of the per-user secret store (Fernet). Losing it makes every
# stored secret unreadable; it is not derived from anything else.
OF_SECRETS_KEY={secrets_key}

# Telegram is optional — a fast way to see the agent work in a real messenger.
# Add the token from @BotFather and your own numeric id, then restart:
# OF_TELEGRAM_BOT_TOKEN=123456:ABC-...
# OF_TELEGRAM_ADMIN_IDS=123456
"""

DONE_MESSAGE = """\
Wrote {path} with a generated operator credential:

    username: {username}
    password: {password}

That password is shown here and nowhere else — only its hash went into {path}.
"""

MISSING_KEY_MESSAGE = """\
{path} still has OF_LLM_API_KEY={placeholder}. Put a real key there (any
OpenAI-compatible endpoint) and run this again.
"""

EXISTING_OK_MESSAGE = "{path} already exists and looks usable; leaving it alone."
EXISTING_GAPS_MESSAGE = "{path} already exists, but it is missing:\n{gaps}"
GAP_HINT = """
Fill those in (see .env.example), or move {path} aside and run this again to
generate a fresh one.
"""

# What has to be present for the stack to answer at all.
REQUIRED = (
    ("OF_LLM_API_KEY", "the LLM endpoint key — nothing can be generated without it"),
    (
        "OF_ADMIN_PASSWORD_HASH",
        "the operator credential — the HTTP surface answers 503 without it",
    ),
)


def hash_password(password: str) -> str:
    """Return a `scheme:iterations:salt:digest` value for `OF_ADMIN_PASSWORD_HASH`."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ITERATIONS)
    return HASH_SEPARATOR.join(
        (
            HASH_SCHEME,
            str(HASH_ITERATIONS),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def fernet_key() -> str:
    """Generate a Fernet master key (32 random bytes, urlsafe base64)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(FERNET_KEY_BYTES)).decode()


def read_values(text: str) -> dict[str, str]:
    """Parse `KEY=value` lines of an env file, ignoring comments and blanks."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def missing_requirements(values: dict[str, str]) -> list[str]:
    """List the human-readable gaps that keep the given env from working."""
    return [
        f"  {key} — {why}"
        for key, why in REQUIRED
        if not values.get(key) or values[key] == PLACEHOLDER_KEY
    ]


def render_env(
    llm_base_url: str, llm_api_key: str, llm_model: str, embedding_model: str
) -> tuple[str, str]:
    """Render the env file for a first run; returns it with the shown-once password."""
    password = secrets.token_urlsafe(PASSWORD_BYTES)
    return ENV_TEMPLATE.format(
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        embedding_model=embedding_model,
        admin_username=ADMIN_USERNAME,
        admin_password_hash=hash_password(password),
        secrets_key=fernet_key(),
    ), password


def ask(prompt: str, default: str) -> str:
    """Ask on a terminal, fall back to the default when there is none."""
    if not sys.stdin.isatty():
        return default
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def resolve_key(explicit: str | None) -> str:
    """Take the LLM key from the flag, the environment, or a terminal prompt."""
    if explicit:
        return explicit
    from_env = os.environ.get("OF_LLM_API_KEY", "").strip()
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        return PLACEHOLDER_KEY
    return input(
        f"LLM API key (any OpenAI-compatible endpoint) [{PLACEHOLDER_KEY}]: "
    ).strip() or (PLACEHOLDER_KEY)


def existing_env(path: Path) -> int:
    """Report on an env file that is already there; never overwrite it."""
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
    """Generate or validate the env file; non-zero means "not ready to start"."""
    parser = argparse.ArgumentParser(description="Prepare .env for a first run.")
    parser.add_argument(
        "--env", default=str(ENV_PATH), help="env file to write (default: .env)"
    )
    parser.add_argument(
        "--llm-key", help="LLM API key; asked for interactively when omitted"
    )
    parser.add_argument(
        "--llm-base-url", help=f"LLM base URL (default: {DEFAULT_LLM_BASE_URL})"
    )
    parser.add_argument("--llm-model", help=f"model id (default: {DEFAULT_LLM_MODEL})")
    parser.add_argument(
        "--embedding-model",
        default="text-embedding-3-small",
        help="embedding model served by the same endpoint",
    )
    args = parser.parse_args(argv)

    path = Path(args.env)
    if path.exists():
        return existing_env(path)

    base_url = args.llm_base_url or ask("LLM base URL", DEFAULT_LLM_BASE_URL)
    model = args.llm_model or ask("LLM model", DEFAULT_LLM_MODEL)
    key = resolve_key(args.llm_key)
    content, password = render_env(base_url, key, model, args.embedding_model)
    path.write_text(content, encoding="utf-8")
    # The credential is useless to an attacker who cannot read .env anyway, but
    # the file still holds a master key: keep it owner-only where the OS allows.
    path.chmod(0o600)
    print(DONE_MESSAGE.format(path=path, username=ADMIN_USERNAME, password=password))
    if key == PLACEHOLDER_KEY:
        print(
            MISSING_KEY_MESSAGE.format(path=path, placeholder=PLACEHOLDER_KEY),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
