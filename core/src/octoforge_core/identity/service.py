"""Admission: who may talk, decided from statuses and the operator's cap.

The identity module must not import the settings module (modules meet only
in the composition root), so the cap arrives as a read callable — the
composition wires it over the settings store.
"""

from collections.abc import Awaitable, Callable

from octoforge_core.identity.api import IdentityStore, UserStatus

#: Reads the current active-user cap; None = no cap.
ActiveCapReader = Callable[[], Awaitable[int | None]]


class AccessService:
    """Answers "may this person talk right now?" at every surface's door.

    Everyone is born WAITING; `admit` promotes them the moment a slot under
    the cap is free — at first contact or on any later message, so raising
    the cap needs no sweep: the queue drains as its people knock. What it
    never does is demote or auto-promote anyone else; freed slots are handed
    out by the operator's hand or by the next knock, not by a background job.
    """

    def __init__(self, identity: IdentityStore, cap: ActiveCapReader) -> None:
        self._identity = identity
        self._cap = cap

    async def admit(self, user_id: str) -> UserStatus:
        """The person's current standing, after one activation attempt.

        ACTIVE and BANNED answer immediately — the cap is read only for the
        WAITING, so the settled majority never pays the settings lookup.
        """
        user = await self._identity.get_user(user_id)
        if user.status is not UserStatus.WAITING:
            return user.status
        if await self._identity.try_activate(user_id, await self._cap()):
            return UserStatus.ACTIVE
        return UserStatus.WAITING
