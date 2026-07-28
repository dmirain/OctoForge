"""Tests for the dialog-facing ask_user tool."""

import pytest

from octoforge_core.dialogs.tools import (
    ASK_ACK,
    EMPTY_QUESTION_ERROR,
    NO_EXCHANGE_ERROR,
    NO_PROMPTER_ERROR,
    QUESTION_PARAM,
    AskUserTool,
)
from octoforge_core.tools.base import ToolContext

USER_ID = "user-1"
CHANNEL = "web"
DIALOG_ID = "dlg-1"
QUESTION = "which city?"
CTX_NO_PROMPTER = ToolContext(user_id=USER_ID, channel=CHANNEL, dialog_id=DIALOG_ID)


class FakePrompter:
    """UserPrompter stub with a programmed outcome, recording asked questions."""

    def __init__(self, outcome: bool) -> None:
        self.outcome = outcome
        self.asked: list[str] = []

    async def ask(self, question: str) -> bool:
        self.asked.append(question)
        return self.outcome


def ctx_with(prompter: FakePrompter) -> ToolContext:
    return ToolContext(
        user_id=USER_ID,
        channel=CHANNEL,
        dialog_id=DIALOG_ID,
        user_prompter=prompter,
    )


async def test_asking_without_a_prompter_is_refused() -> None:
    tool = AskUserTool()

    result = await tool.execute({QUESTION_PARAM: QUESTION}, CTX_NO_PROMPTER)

    assert result == NO_PROMPTER_ERROR


@pytest.mark.parametrize("question", ["", "   "])
async def test_empty_or_whitespace_question_is_refused(question: str) -> None:
    tool = AskUserTool()
    prompter = FakePrompter(True)

    result = await tool.execute({QUESTION_PARAM: question}, ctx_with(prompter))

    assert result == EMPTY_QUESTION_ERROR
    assert prompter.asked == []  # never reached the prompter


async def test_prompter_declining_reports_no_exchange() -> None:
    """A RUN/cron process has no exchange to park: ask() returns False."""
    tool = AskUserTool()
    prompter = FakePrompter(False)

    result = await tool.execute({QUESTION_PARAM: QUESTION}, ctx_with(prompter))

    assert result == NO_EXCHANGE_ERROR
    assert prompter.asked == [QUESTION]


async def test_prompter_accepting_delivers_the_question() -> None:
    tool = AskUserTool()
    prompter = FakePrompter(True)

    result = await tool.execute({QUESTION_PARAM: f"  {QUESTION}  "}, ctx_with(prompter))

    assert result == ASK_ACK
    assert prompter.asked == [QUESTION]  # stripped before it reaches the prompter
