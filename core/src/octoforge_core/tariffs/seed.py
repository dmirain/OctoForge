"""The starter plans an installation is born with.

Two plans, seeded as data rather than assumed by code: `unlimited`, which is
what everyone had before plans existed, and `freemium`, the one to make
default when the installation opens to the public. Both are ordinary rows —
the operator edits every number from the console afterwards, and this module
never touches a plan that already exists.

**Neither is the default here.** A brand-new installation behaves exactly as
before — no binding, no limits — because somebody self-hosting for their own
team must not discover a cap they never set. Opening to the public is the
operator's decision: mark `freemium` default in the console. (The migration
that introduces these plans does mark it, but only for an installation that
already has people — there, the newcomers are strangers and everyone present
is grandfathered onto `unlimited` in the same step.)

Seeding lives here, and not only in that migration, because a fresh
non-SQLite database skips the migration chain entirely (`create_all` +
`stamp head`, see `composition_schema.py`); this way a new Postgres installation is
not left with an empty plan catalog its SQLite twin would have.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy import Connection

from octoforge_core.db.base import UTCDateTime
from octoforge_core.time import utc_now

UNLIMITED_CODE = "unlimited"
FREEMIUM_CODE = "freemium"

#: Every core feature code as of this seed. Spelled out rather than imported
#: from `FeatureCode`: a plan is data, and a later core feature must not
#: silently widen a plan an operator has already reviewed.
_UNLIMITED_FEATURES = [
    "skill_create",
    "voice_transcription",
    "web_search",
    "mcp_add",
    "http_endpoints",
    "vision",
]
#: What a stranger gets for free: the conversation itself and web search.
#: Everything metered per unit (vision, voice) and everything that reaches
#: outward on the user's behalf (MCP, HTTP contracts) stays off the free tier.
_FREEMIUM_FEATURES = ["web_search"]

_tariffs = sa.table(
    "tariffs",
    sa.column("id", sa.String),
    sa.column("code", sa.String),
    sa.column("title", sa.String),
    sa.column("features", sa.JSON),
    sa.column("daily_tokens", sa.Integer),
    sa.column("daily_user_messages", sa.Integer),
    sa.column("daily_assistant_messages", sa.Integer),
    sa.column("max_cron_jobs", sa.Integer),
    sa.column("max_datasets", sa.Integer),
    sa.column("max_memory_chars", sa.Integer),
    sa.column("is_default", sa.Boolean),
    # the model's own type, not a bare DateTime: Postgres keeps a timestamptz
    # here and asyncpg refuses an aware value bound to anything else
    sa.column("created_at", UTCDateTime),
    sa.column("updated_at", UTCDateTime),
)

_STARTER_PLANS = (
    {
        "code": UNLIMITED_CODE,
        "title": "Unlimited",
        "features": _UNLIMITED_FEATURES,
        "daily_tokens": None,
        "daily_user_messages": None,
        "daily_assistant_messages": None,
        "max_cron_jobs": None,
        "max_datasets": None,
        "max_memory_chars": None,
        "is_default": False,
    },
    {
        "code": FREEMIUM_CODE,
        "title": "Freemium",
        "features": _FREEMIUM_FEATURES,
        "daily_tokens": 100_000,
        "daily_user_messages": 30,
        "daily_assistant_messages": 60,
        "max_cron_jobs": 1,
        "max_datasets": 1,
        "max_memory_chars": 4_000,
        "is_default": False,
    },
)


def seed_starter_tariffs(connection: Connection) -> None:
    """Insert the starter plans that are missing; never edit an existing one.

    Idempotent and deliberately timid: a code that already exists is left
    exactly as the operator left it, and neither plan claims the default
    flag — see the module docstring.
    """
    if not sa.inspect(connection).has_table("tariffs"):
        return
    present = {code for (code,) in connection.execute(sa.select(_tariffs.c.code))}
    now = utc_now()
    rows = [
        plan | {"id": uuid.uuid4().hex, "created_at": now, "updated_at": now}
        for plan in _STARTER_PLANS
        if plan["code"] not in present
    ]
    if rows:
        connection.execute(sa.insert(_tariffs), rows)
