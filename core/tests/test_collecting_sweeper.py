"""Tests for CollectingSweeper: nominates settled material collections.

The sweep itself does no staleness filtering (that's `list_stale_collecting`'s
job, covered in test_dialogs_store.py) — it just hands whatever the
repository reports over to the promoter, one dialog at a time, and must not
let one broken dialog stop the rest.
"""

from octoforge_core.agent.collecting import CollectingSweeper
from octoforge_core.dialogs.api import Exchange, ExchangeList, ExchangeStatus
from octoforge_core.domain import Dialog
from octoforge_core.time import utc_now

USER_ID = "user-1"
OTHER_USER_ID = "user-2"
BROKEN_USER_ID = "user-broken"
CHANNEL = "web"
DIALOG_ID = "dlg-1"
OTHER_DIALOG_ID = "dlg-2"
BROKEN_DIALOG_ID = "dlg-broken"
EXCHANGE_ID = "exch-1"
OTHER_EXCHANGE_ID = "exch-2"
BROKEN_EXCHANGE_ID = "exch-broken"
EXCHANGE_TITLE = "forwarded material"
QUIET_SECONDS = 30.0


def make_exchange(exchange_id: str, dialog_id: str) -> Exchange:
    now = utc_now()
    return Exchange(
        id=exchange_id,
        dialog_id=dialog_id,
        status=ExchangeStatus.COLLECTING,
        title=EXCHANGE_TITLE,
        created_at=now,
        updated_at=now,
    )


def make_dialog(dialog_id: str, user_id: str) -> Dialog:
    now = utc_now()
    return Dialog(id=dialog_id, user_id=user_id, channel=CHANNEL, created_at=now, updated_at=now)


class FakeExchangeRepository:
    """Stub over the one ExchangeRepository method the sweep calls."""

    def __init__(self, stale: ExchangeList) -> None:
        self._stale = stale
        self.recorded_quiet_seconds: float | None = None

    async def list_stale_collecting(self, quiet_seconds: float) -> ExchangeList:
        self.recorded_quiet_seconds = quiet_seconds
        return list(self._stale)


class FakeDialogRepository:
    """Stub resolving dialog_id -> Dialog for the one method the sweep calls."""

    def __init__(self, dialogs: dict[str, Dialog]) -> None:
        self._dialogs = dialogs

    async def get(self, dialog_id: str) -> Dialog:
        return self._dialogs[dialog_id]


class RecordingPromoter:
    """CollectionPromoter stub recording every promotion; one user id can fail."""

    def __init__(self, fail_user_id: str | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._fail_user_id = fail_user_id

    async def promote_collection(self, user_id: str, channel: str, exchange_id: str) -> None:
        if user_id == self._fail_user_id:
            raise RuntimeError("promoter boom")
        self.calls.append((user_id, channel, exchange_id))


async def test_tick_promotes_every_stale_collection_through_the_promoter() -> None:
    stale = [
        make_exchange(EXCHANGE_ID, DIALOG_ID),
        make_exchange(OTHER_EXCHANGE_ID, OTHER_DIALOG_ID),
    ]
    dialogs = FakeDialogRepository(
        {
            DIALOG_ID: make_dialog(DIALOG_ID, USER_ID),
            OTHER_DIALOG_ID: make_dialog(OTHER_DIALOG_ID, OTHER_USER_ID),
        }
    )
    promoter = RecordingPromoter()
    sweeper = CollectingSweeper(
        FakeExchangeRepository(stale), dialogs, promoter, quiet_seconds=QUIET_SECONDS
    )

    promoted = await sweeper.tick()

    assert promoted == len(stale)
    assert sorted(promoter.calls) == sorted(
        [
            (USER_ID, CHANNEL, EXCHANGE_ID),
            (OTHER_USER_ID, CHANNEL, OTHER_EXCHANGE_ID),
        ]
    )


async def test_tick_passes_its_quiet_window_through_and_promotes_only_whats_reported() -> None:
    """The sweep does no filtering of its own — a "fresh" collection the
    repository doesn't report is simply never handed to the promoter."""
    exchanges = FakeExchangeRepository([make_exchange(EXCHANGE_ID, DIALOG_ID)])
    dialogs = FakeDialogRepository({DIALOG_ID: make_dialog(DIALOG_ID, USER_ID)})
    promoter = RecordingPromoter()
    sweeper = CollectingSweeper(exchanges, dialogs, promoter, quiet_seconds=QUIET_SECONDS)

    promoted = await sweeper.tick()

    assert exchanges.recorded_quiet_seconds == QUIET_SECONDS
    assert promoted == 1
    assert promoter.calls == [(USER_ID, CHANNEL, EXCHANGE_ID)]


async def test_tick_without_stale_collections_promotes_nothing() -> None:
    sweeper = CollectingSweeper(
        FakeExchangeRepository([]), FakeDialogRepository({}), RecordingPromoter()
    )

    assert await sweeper.tick() == 0


async def test_tick_keeps_going_when_one_dialogs_promoter_raises() -> None:
    stale = [
        make_exchange(BROKEN_EXCHANGE_ID, BROKEN_DIALOG_ID),
        make_exchange(EXCHANGE_ID, DIALOG_ID),
    ]
    dialogs = FakeDialogRepository(
        {
            BROKEN_DIALOG_ID: make_dialog(BROKEN_DIALOG_ID, BROKEN_USER_ID),
            DIALOG_ID: make_dialog(DIALOG_ID, USER_ID),
        }
    )
    promoter = RecordingPromoter(fail_user_id=BROKEN_USER_ID)
    sweeper = CollectingSweeper(FakeExchangeRepository(stale), dialogs, promoter)

    promoted = await sweeper.tick()

    # the broken dialog is skipped, not fatal: the healthy one still lands
    assert promoted == 1
    assert promoter.calls == [(USER_ID, CHANNEL, EXCHANGE_ID)]
