"""Rendering a process branch from the narrative and the exchange structure.

One rule, one place. A run's task used to be implied by position ("your
question is the last message"), which broke the moment answers could arrive
out of order, and was patched with three ad-hoc envelopes plus in-memory id
sets. Here the marks are derived from durable exchange state instead:

- the run's own question -> "your task";
- later messages of the same exchange -> clarifications of that task;
- questions of OTHER live exchanges -> being handled elsewhere;
- everything else (closed exchanges, legacy rows, assistant messages) ->
  plain history.

Marks live in the branch copy only: the narrative and the store keep the
clean text, exactly like the date envelope.
"""

from dataclasses import replace

from octoforge_core.domain import ChatMessage, MessageRole

TASK_NOTE_TEMPLATE = "[YOUR TASK — this is the message you must answer in this run]\n{content}"
CLARIFICATION_NOTE_TEMPLATE = (
    "[Clarification for your task — account for it in your answer]\n{content}"
)
OTHER_TASK_NOTE_TEMPLATE = "[Another run is answering this — do not answer it here]\n{content}"


def render_branch(
    messages: list[ChatMessage],
    own_exchange_id: str | None,
    live_exchange_ids: frozenset[str],
) -> list[ChatMessage]:
    """Return a marked copy of the narrative for one run's branch.

    `own_exchange_id` is the exchange this run owes an answer to (None for
    self-contained RUN tasks); `live_exchange_ids` are the exchanges other
    runs are currently answering.
    """
    rendered: list[ChatMessage] = []
    own_seen = False
    for message in messages:
        mark = None
        if message.role is MessageRole.USER and message.exchange_id is not None:
            if message.exchange_id == own_exchange_id:
                mark = CLARIFICATION_NOTE_TEMPLATE if own_seen else TASK_NOTE_TEMPLATE
                own_seen = True
            elif message.exchange_id in live_exchange_ids:
                mark = OTHER_TASK_NOTE_TEMPLATE
        if mark is None:
            rendered.append(message)
        else:
            rendered.append(replace(message, content=mark.format(content=message.content)))
    return rendered
