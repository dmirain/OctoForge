"""Request-scoped dialog actor identity and manager."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from octoforge_core import ConversationManager

from octoforge_server.deps import get_channel, get_conversation_manager, get_user_id


@dataclass(frozen=True, slots=True)
class DialogActor:
    manager: Annotated[ConversationManager, Depends(get_conversation_manager)]
    user_id: Annotated[str, Depends(get_user_id)]
    channel: Annotated[str, Depends(get_channel)]


DialogActorDep = Annotated[DialogActor, Depends()]
