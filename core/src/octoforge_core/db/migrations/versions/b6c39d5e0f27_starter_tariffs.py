"""starter_tariffs: seed the unlimited and freemium plans, grandfather today's users

Opening an installation to the public turns "no binding = no limits" from a
convenience into a giveaway: the freemium plan becomes the default, and every
newcomer lands on it. The people already here predate that decision and must
not be moved onto a free tier by an upgrade, so this migration binds each of
them to the `unlimited` plan explicitly — a binding beats the default, and
the operator can move anyone off it from the console afterwards.

One-shot by construction: only users that exist at migration time, and only
those without a binding of their own, are touched. A fresh non-SQLite
database never runs this (`create_all` + `stamp head`), which is why the same
plans are also seeded from `tariffs/seed.py` on that path; the rows are
spelled out here rather than imported from it because a migration is a frozen
snapshot — and because `db/` does not import domain modules. There is nobody
to grandfather on an empty database, so only the seeding is shared.

Revision ID: b6c39d5e0f27
Revises: a7f2d84c61b9
Create Date: 2026-08-07

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

import octoforge_core.db.base

revision: str = "b6c39d5e0f27"
down_revision: str | None = "a7f2d84c61b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNLIMITED_CODE = "unlimited"
FREEMIUM_CODE = "freemium"

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
    sa.column("created_at", octoforge_core.db.base.UTCDateTime),
    sa.column("updated_at", octoforge_core.db.base.UTCDateTime),
)
_users = sa.table("users", sa.column("id", sa.String))
_user_tariffs = sa.table(
    "user_tariffs",
    sa.column("id", sa.String),
    sa.column("user_id", sa.String),
    sa.column("tariff_id", sa.String),
    sa.column("assigned_at", octoforge_core.db.base.UTCDateTime),
)

_PLANS = (
    {
        "code": UNLIMITED_CODE,
        "title": "Unlimited",
        "features": [
            "skill_create",
            "voice_transcription",
            "web_search",
            "mcp_add",
            "http_endpoints",
            "vision",
        ],
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
        "features": ["web_search"],
        "daily_tokens": 100_000,
        "daily_user_messages": 30,
        "daily_assistant_messages": 60,
        "max_cron_jobs": 1,
        "max_datasets": 1,
        "max_memory_chars": 4_000,
        "is_default": False,
    },
)


def _seed_plans(connection: sa.Connection, *, freemium_by_default: bool) -> None:
    """Insert the missing starter plans; never edit one that already exists.

    `freemium` claims the default flag only while no other plan holds it: at
    most one default is an invariant of the store, and a seed must not take
    it from a plan somebody chose on purpose.
    """
    present = {code for (code,) in connection.execute(sa.select(_tariffs.c.code))}
    taken = bool(
        connection.execute(
            sa.select(_tariffs.c.code).where(_tariffs.c.is_default.is_(True))
        ).first()
    )
    now = datetime.now(UTC)
    rows = [
        plan | {"id": uuid.uuid4().hex, "created_at": now, "updated_at": now}
        for plan in _PLANS
        if plan["code"] not in present
    ]
    for row in rows:
        row["is_default"] = (
            row["code"] == FREEMIUM_CODE and freemium_by_default and not taken
        )
    if rows:
        connection.execute(sa.insert(_tariffs), rows)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not all(inspector.has_table(table) for table in ("tariffs", "users", "user_tariffs")):
        return
    people = [user_id for (user_id,) in connection.execute(sa.select(_users.c.id))]
    # An installation that already has people is one being opened to the
    # public: freemium becomes what newcomers land on, while everybody
    # present is grandfathered below. A brand-new installation gets the two
    # plans ready to use but no default at all — somebody self-hosting for
    # their own team must not discover a 30-messages-a-day cap they never set.
    _seed_plans(connection, freemium_by_default=bool(people))
    unlimited = connection.execute(
        sa.select(_tariffs.c.id).where(_tariffs.c.code == UNLIMITED_CODE)
    ).scalar_one_or_none()
    if unlimited is None:  # an installation that owns the code and deleted the plan
        return
    bound = {user_id for (user_id,) in connection.execute(sa.select(_user_tariffs.c.user_id))}
    now = datetime.now(UTC)
    rows = [
        {"id": uuid.uuid4().hex, "user_id": user_id, "tariff_id": unlimited, "assigned_at": now}
        for user_id in people
        if user_id not in bound
    ]
    if rows:
        connection.execute(sa.insert(_user_tariffs), rows)


def downgrade() -> None:
    """Drop the seeded plans and every binding to them.

    Bindings first: a `user_tariffs` row would otherwise point at a plan that
    no longer exists.
    """
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not all(inspector.has_table(table) for table in ("tariffs", "user_tariffs")):
        return
    seeded = [
        tariff_id
        for (tariff_id,) in connection.execute(
            sa.select(_tariffs.c.id).where(_tariffs.c.code.in_((UNLIMITED_CODE, FREEMIUM_CODE)))
        )
    ]
    if not seeded:
        return
    connection.execute(sa.delete(_user_tariffs).where(_user_tariffs.c.tariff_id.in_(seeded)))
    connection.execute(sa.delete(_tariffs).where(_tariffs.c.id.in_(seeded)))
