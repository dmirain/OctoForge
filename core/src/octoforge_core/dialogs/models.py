"""ORM models of the dialogs module; the tables are owned by this module.

The messages table is additionally read (never written) by two documented
exceptions: `context/store.py` (the archive view) and `admin/store.py`
(the cross-user read model).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class DialogRow(Base):
    """A dialog line owned by the unique (user_id, channel) pair."""

    __tablename__ = "dialogs"
    __table_args__ = (UniqueConstraint("user_id", "channel"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class MessageRow(Base):
    """One message of a dialog, ordered by seq within the dialog."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("dialog_id", "seq"),
        UniqueConstraint(
            "dialog_id", "client_message_id", name="uq_messages_dialog_client_message"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    dialog_id: Mapped[str] = mapped_column(ForeignKey("dialogs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(String)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    client_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # the exchange this message belongs to: a user question opens one, its
    # clarifications and the answer join it. NULL on legacy rows and on
    # messages of RUN tasks (cron/spawned work owes the user nothing).
    exchange_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # MessageKind for the exceptional kinds only: NULL means the user's own
    # words, so legacy rows keep their meaning without a backfill
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    # files that came with the message, as transport-scoped references
    # ([{kind, ref}]); NULL when the message carried none
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class DialogClaimRow(Base):
    """Which process runs a dialog's actor, and since which generation.

    One row per dialog, taken when a process builds the runner. `generation`
    grows on every claim, so a previous owner can tell that it was preempted
    without anyone having to reach it: it compares the number it was born
    with against the stored one. `heartbeat_at` exists for the other
    question — whether an owner is still alive — which is what recovery needs
    before it may touch a dialog's stranded work.

    Absent row means nobody has ever run this dialog on a build that recorded
    claims; it is free to take.
    """

    __tablename__ = "dialog_claims"

    dialog_id: Mapped[str] = mapped_column(ForeignKey("dialogs.id"), primary_key=True)
    owner: Mapped[str] = mapped_column(String)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    # indexed: recovery filters by it over every dialog holding stranded work
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)


class ExchangeRow(Base):
    """One obligation to the user: a question, its clarifications and its answer.

    Task status and exchange status are deliberately separate: a run may end
    (DONE) without closing the exchange — asking the user something back is
    exactly that case.
    """

    __tablename__ = "exchanges"
    # the composite backs list_live, the hottest query in the runtime: it
    # filters by both columns on every message and every loop iteration
    __table_args__ = (Index("ix_exchanges_dialog_status", "dialog_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    dialog_id: Mapped[str] = mapped_column(ForeignKey("dialogs.id"), index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    # the question the agent asked back, kept for the reminder text
    pending_question: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
