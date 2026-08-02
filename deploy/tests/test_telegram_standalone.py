"""Tests for the standalone Telegram surface (no HTTP listener involved)."""

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

import pytest
from octoforge_core.agent.runner import ConversationManager
from octoforge_core.config import EmbeddingBackend
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.models import DialogClaimRow
from octoforge_core.dialogs.store import SqlAlchemyDialogRepository
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_server.channels import WEB_CHANNEL
from octoforge_server.config import Settings
from octoforge_telegram.client import TELEGRAM_CHANNEL
from octoforge_telegram.surface import _report_failure
from sqlalchemy import select

from octoforge_deploy.main import runtime
from octoforge_deploy.telegram_only import NO_TOKEN_MESSAGE, run_standalone

#: how many Telegram dialogs the claim test seeds before the pod comes up
EXPECTED_SEEDED = 3
SURFACE_LOGGER = "octoforge_telegram.surface"
RENDER_ONLY_MESSAGE = "rendering only"
LOG_WAIT_SECONDS = 5.0
LOG_POLL_SECONDS = 0.01


async def _wait_for_log(caplog: pytest.LogCaptureFixture, needle: str) -> None:
    """Block until `needle` shows up in the captured log, or fail saying it never did."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOG_WAIT_SECONDS
    while loop.time() < deadline:
        if any(needle in record.message for record in caplog.records):
            return
        await asyncio.sleep(LOG_POLL_SECONDS)
    raise AssertionError(f"{needle!r} never reached the log")


def settings_for(tmp_path: Path, token: str = "") -> Settings:
    """Settings wired to a throwaway database; no external calls are made."""
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        telegram_bot_token=token,
        embedding_backend=EmbeddingBackend.OPENAI,
        embedding_api_key="",
    )


async def test_run_standalone_requires_token(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=NO_TOKEN_MESSAGE):
        await run_standalone(settings_for(tmp_path))


async def test_runtime_builds_without_fastapi(tmp_path: Path) -> None:
    """The shared composition root works with no FastAPI app and no bot token."""
    async with runtime(settings_for(tmp_path)) as rt:
        assert rt.conversation_manager is not None
        assert rt.channel == WEB_CHANNEL
        assert rt.cron_store is not None


async def test_a_starting_pod_claims_no_dialog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Coming up is not a reason to take a dialog away from whoever holds it.

    Startup used to build a bridge for every known Telegram dialog so a
    scheduled run would have somewhere to land. A bridge cannot exist without
    its dialog's actor, and building that actor is what CLAIMS the dialog — so
    every pod took every dialog on every start and the last one to boot owned
    them all, cutting off whatever its peer was mid-answer on. With a single
    pod this is invisible: there is nobody to take them from.

    Asserted through the real composition root, because the coupling being
    guarded is between the surface and the dialog store, and only the root
    puts those two together. Asserted on the claim table rather than on the
    manager's runners, because the claim is what peers read.
    """
    monkeypatch.setenv("OF_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OF_TELEGRAM_POLL_IN_PROCESS", "false")
    settings = settings_for(tmp_path)

    engine = create_engine(settings.database_url)
    await init_db(engine)
    seeded = create_session_factory(engine)
    dialogs = SqlAlchemyDialogRepository(seeded)
    identities = SqlAlchemyIdentityStore(seeded)
    # a person with a Telegram identity, exactly as an existing user looks:
    # a dialog whose chat cannot be found is skipped, so seeding the dialog
    # alone would make any startup sweep look harmless whether it is or not
    for account in (101, 102, 103):
        person = await identities.resolve_or_create(TELEGRAM_CHANNEL, str(account))
        await dialogs.get_or_create(person, TELEGRAM_CHANNEL)
    await engine.dispose()

    with caplog.at_level(logging.INFO, logger=SURFACE_LOGGER):
        async with runtime(settings) as rt:
            # This line is the surface's last act on the rendering-only path,
            # and the sweep this test forbids ran BEFORE it. Waiting for it is
            # what makes "nothing was claimed" mean anything: a couple of loop
            # ticks would pass whether the sweep happens or not, since it is
            # several database round trips long.
            await _wait_for_log(caplog, RENDER_ONLY_MESSAGE)
            async with rt.session_factory() as session:
                claimed = (await session.scalars(select(DialogClaimRow.dialog_id))).all()
            assert list(claimed) == []
            assert len(await rt.dialogs.list_by_channel(TELEGRAM_CHANNEL)) == EXPECTED_SEEDED


async def test_recovery_runs_with_the_renderers_already_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order of two lines in the composition root, which nothing else states.

    Recovery builds the actor of every dialog it picks up, and the manager
    attaches the surface to each actor it builds — so a renderer registered
    afterwards is registered too late for exactly the dialogs that had work
    waiting. Startup warming used to paper over this by preparing a bridge
    per dialog; nothing does now.
    """
    seen: list[bool] = []
    recover = ConversationManager.recover_interrupted

    async def spy(self: ConversationManager) -> None:
        seen.append(self._surface is not None)
        await recover(self)

    monkeypatch.setattr(ConversationManager, "recover_interrupted", spy)
    monkeypatch.setenv("OF_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OF_TELEGRAM_POLL_IN_PROCESS", "false")

    async with runtime(settings_for(tmp_path)):
        pass

    assert seen == [True]


# --- the telegram task's done-callback (supervisor-lite) --------------------


async def test_a_dying_surface_is_reported_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception escaping the Telegram surface task is logged loudly, not dropped."""

    async def explode() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(explode())
    with suppress(RuntimeError):
        await task  # let it finish (and record its exception) before inspecting it

    with caplog.at_level(logging.ERROR):
        _report_failure(task)

    assert any("telegram surface stopped" in record.message for record in caplog.records)


async def test_a_cancelled_surface_is_not_a_failure() -> None:
    """Normal shutdown (task.cancel() from `_stop_background_tasks`) is not a failure."""

    async def spin_forever() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(spin_forever())
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    _report_failure(task)  # must not raise (task.exception() would, on cancel)
