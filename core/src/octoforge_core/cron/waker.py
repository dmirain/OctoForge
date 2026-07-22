"""Adapter from the cron waker port to the conversation manager (in-process wake)."""

from octoforge_core.agent.runner import ConversationManager


class ManagerCronWaker:
    """Delivers due cron jobs as background processes of the user's dialog."""

    def __init__(self, manager: ConversationManager) -> None:
        self._manager = manager

    async def wake(
        self,
        user_id: str,
        channel: str,
        title: str,
        prompt: str,
        cron_job_id: str,
    ) -> bool:
        """Get-or-create the user's runner and start the job's process in it.

        Returns whether the process was actually started (see `ConversationManager.wake`).
        """
        return await self._manager.wake(user_id, channel, title, prompt, cron_job_id)
