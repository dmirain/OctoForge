"""The console's Telegram page, owned by the Telegram surface.

Who a `tg:<id>` actually is lives in this surface's own store, not in core's
read model — the entity is transport-specific. The console used to reach in
and read it directly, which quietly made the operator console depend on
Telegram: a deployment with Slack instead would have carried a route it could
not serve.

So the route ships with the surface that can answer it. The console renders
whatever pages the installed surfaces contribute, and a deployment without a
bot simply has no Telegram page.

Temporary in one sense: once identity is a core concept — one user, several
surface identities — "who is this" becomes a core read and this route goes
away. Until then it stays here rather than in the console.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from octoforge_web.deps import get_telegram_invites, get_telegram_members, require_admin
from octoforge_web.telegram.invites.api import InviteStore, MemberDirectory

# `require_admin` exactly as on the console's own router: defense in depth
# behind the middleware, and the same gate object, so a request that already
# authenticated does not hash a second time. Moving a route must not quietly
# move it out from behind its guard.
router = APIRouter(prefix="/api/admin/telegram", dependencies=[Depends(require_admin)])

MembersDep = Annotated["MemberDirectory | None", Depends(get_telegram_members)]
InvitesDep = Annotated["InviteStore | None", Depends(get_telegram_invites)]


@router.get("/users")
async def telegram_users(members: MembersDep, invites: InvitesDep) -> dict[str, Any]:
    """Telegram who-is-who: profiles recorded at the gate, with invite attribution.

    Empty when the bot is not configured — the surface is installed but has
    nothing to show.
    """
    if members is None:
        return {"items": [], "total": 0}
    invite_by_user: dict[str, Any] = {}
    if invites is not None:
        invite_by_user = {
            invite.claimed_by: invite
            for invite in await invites.list_all()
            if invite.claimed_by is not None
        }
    items = []
    for profile in await members.list_all():
        invite = invite_by_user.get(profile.user_id)
        items.append(
            {
                "user_id": profile.user_id,
                "first_name": profile.first_name,
                "last_name": profile.last_name,
                "username": profile.username,
                "display_name": profile.display_name,
                "invite_code": invite.code if invite is not None else None,
                "invite_note": invite.note if invite is not None else None,
                "invite_status": invite.status.value if invite is not None else None,
                "first_seen_at": profile.first_seen_at.isoformat(),
                "last_seen_at": profile.last_seen_at.isoformat(),
            }
        )
    return {"items": items, "total": len(items)}
