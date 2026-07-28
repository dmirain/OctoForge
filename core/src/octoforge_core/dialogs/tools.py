"""Dialog-facing tools: asking the user something back."""

from typing import Any

from octoforge_core.tools.base import ToolContext, ToolSpec

ASK_USER_TOOL_NAME = "ask_user"
QUESTION_PARAM = "question"
ASK_ACK = (
    "Question delivered to the user. End this run now without writing anything else — "
    "you will be started again with their reply."
)
NO_PROMPTER_ERROR = "asking the user is not available in this context"
EMPTY_QUESTION_ERROR = "question must not be empty"
NO_EXCHANGE_ERROR = (
    "asking the user is unavailable here: this run is a background task, not an "
    "answer to a user question, so a reply would never resume it. The question was "
    "NOT delivered. Finish the task with your best result instead."
)


class AskUserTool:
    """Ask the user a clarifying question and end the run.

    The obligation stays open (its exchange moves to AWAITING_USER) instead
    of being closed by a run that produced no answer: the reply resumes it
    with a fresh run. Asking is an explicit act, not a guess from the text —
    that is what keeps "I asked something" distinguishable from "I answered".
    """

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=ASK_USER_TOOL_NAME,
            description=(
                "Ask the user a clarifying question when you cannot proceed without "
                "their input. Use this instead of writing the question as your answer: "
                "it keeps the task open and resumes it when they reply."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    QUESTION_PARAM: {
                        "type": "string",
                        "description": "What to ask, phrased for the user.",
                    }
                },
                "required": [QUESTION_PARAM],
            },
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.user_prompter is None:
            return NO_PROMPTER_ERROR
        question = str(arguments.get(QUESTION_PARAM, "")).strip()
        if not question:
            return EMPTY_QUESTION_ERROR
        if not await context.user_prompter.ask(question):
            return NO_EXCHANGE_ERROR
        return ASK_ACK
