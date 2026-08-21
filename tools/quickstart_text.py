"""Static first-run environment template and operator messages."""

from pathlib import Path

ENV_PATH = Path(".env")
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
PLACEHOLDER_KEY = "REPLACE_ME"
ADMIN_USERNAME = "admin"

ENV_TEMPLATE = """\
# Written by tools/quickstart.py. See .env.example for every setting.
OF_LLM_BASE_URL={llm_base_url}
OF_LLM_API_KEY={llm_api_key}
OF_LLM_MODEL={llm_model}
OF_EMBEDDING_MODEL={embedding_model}

OF_ADMIN_USERNAME={admin_username}
OF_ADMIN_PASSWORD_HASH={admin_password_hash}
OF_SECRETS_KEY={secrets_key}

# Telegram is optional:
# OF_TELEGRAM_BOT_TOKEN=123456:ABC-...
# OF_TELEGRAM_ADMIN_IDS=123456
"""

DONE_MESSAGE = """\
Wrote {path} with a generated operator credential:

    username: {username}
    password: {password}

That password is shown here and nowhere else - only its hash went into {path}.
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
REQUIRED = (
    ("OF_LLM_API_KEY", "the LLM endpoint key - nothing can be generated without it"),
    ("OF_ADMIN_PASSWORD_HASH", "the operator credential - HTTP answers 503 without it"),
)
