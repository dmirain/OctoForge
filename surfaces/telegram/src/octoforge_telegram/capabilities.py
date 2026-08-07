"""What this surface tells the operator about itself at startup.

The service assembles the report but cannot write this line: it does not know
a bot exists. So the surface says its own piece and the deployment hands it
over, which keeps one list in the log without one package knowing the other.
"""

from octoforge_server.capabilities import Capability

from octoforge_telegram.config import TelegramSettings

NAME = "telegram"


def describe(settings: TelegramSettings) -> Capability:
    """One line: whether the bot runs, and who may talk to it."""
    return Capability(
        name=NAME, enabled=bool(settings.telegram_bot_token), detail=_detail(settings)
    )


def _detail(settings: TelegramSettings) -> str:
    if not settings.telegram_bot_token:
        return "OF_TELEGRAM_BOT_TOKEN is empty — the adapter does not start"
    admins = len(settings.telegram_admin_ids)
    if not admins:
        gate = "OPEN TO EVERYONE (no admin ids)"
    elif settings.telegram_open_registration:
        # a deliberately loud line: which doorman is on duty is the single
        # most consequential thing about a public bot
        gate = f"OPEN REGISTRATION (admission is the active-user cap), {admins} admin(s)"
    else:
        gate = f"invite gate, {admins} admin(s)"
    return f"long-poll, {gate}"
