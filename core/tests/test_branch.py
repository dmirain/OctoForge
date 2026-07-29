"""Tests for render_branch: pure function, no DB or asyncio involved.

Marking rules (see the module docstring of agent/branch.py): the own
exchange's first OWN message is the task, later OWN messages are
clarifications, forwarded material of the own exchange is marked as material
(the last piece becomes the task only when no OWN message ever arrived), and
questions of OTHER live exchanges are dropped from the branch entirely.
"""

from octoforge_core.agent.branch import (
    CLARIFICATION_NOTE_TEMPLATE,
    MATERIAL_NOTE_TEMPLATE,
    MATERIAL_TASK_NOTE_TEMPLATE,
    SHARED_MATERIAL_NOTE_TEMPLATE,
    TASK_NOTE_TEMPLATE,
    render_branch,
)
from octoforge_core.domain import ChatMessage, MessageKind, MessageRole

OWN_EXCHANGE = "exch-own"
FOREIGN_EXCHANGE = "exch-foreign"
NO_LIVE_EXCHANGES: frozenset[str] = frozenset()


def own_message(content: str, exchange_id: str = OWN_EXCHANGE) -> ChatMessage:
    return ChatMessage(role=MessageRole.USER, content=content, exchange_id=exchange_id)


def material_message(content: str, exchange_id: str = OWN_EXCHANGE) -> ChatMessage:
    return ChatMessage(
        role=MessageRole.USER, content=content, kind=MessageKind.MATERIAL, exchange_id=exchange_id
    )


def test_first_own_message_of_the_own_exchange_gets_the_task_mark() -> None:
    messages = [own_message("question")]

    rendered = render_branch(messages, OWN_EXCHANGE, NO_LIVE_EXCHANGES)

    assert rendered[0].content == TASK_NOTE_TEMPLATE.format(content="question")
    # marks live in the branch copy only: the input object is untouched
    assert rendered[0] is not messages[0]
    assert messages[0].content == "question"


def test_later_own_messages_of_the_own_exchange_get_the_clarification_mark() -> None:
    messages = [own_message("question"), own_message("more info"), own_message("even more")]

    rendered = render_branch(messages, OWN_EXCHANGE, NO_LIVE_EXCHANGES)

    assert rendered[0].content == TASK_NOTE_TEMPLATE.format(content="question")
    assert rendered[1].content == CLARIFICATION_NOTE_TEMPLATE.format(content="more info")
    assert rendered[2].content == CLARIFICATION_NOTE_TEMPLATE.format(content="even more")
    assert [m.content for m in messages] == ["question", "more info", "even more"]


def test_material_before_an_own_message_never_steals_the_task_mark() -> None:
    messages = [material_message("forwarded text"), own_message("react to this")]

    rendered = render_branch(messages, OWN_EXCHANGE, NO_LIVE_EXCHANGES)

    assert rendered[0].content == MATERIAL_NOTE_TEMPLATE.format(content="forwarded text")
    assert rendered[1].content == TASK_NOTE_TEMPLATE.format(content="react to this")


def test_material_after_an_own_message_never_steals_the_task_mark() -> None:
    messages = [own_message("react to this"), material_message("forwarded text")]

    rendered = render_branch(messages, OWN_EXCHANGE, NO_LIVE_EXCHANGES)

    assert rendered[0].content == TASK_NOTE_TEMPLATE.format(content="react to this")
    assert rendered[1].content == MATERIAL_NOTE_TEMPLATE.format(content="forwarded text")


def test_material_only_exchange_marks_only_the_last_piece_as_task() -> None:
    messages = [
        material_message("first forward"),
        material_message("second forward"),
        material_message("third forward"),
    ]

    rendered = render_branch(messages, OWN_EXCHANGE, NO_LIVE_EXCHANGES)

    assert rendered[0].content == MATERIAL_NOTE_TEMPLATE.format(content="first forward")
    assert rendered[1].content == MATERIAL_NOTE_TEMPLATE.format(content="second forward")
    assert rendered[2].content == MATERIAL_TASK_NOTE_TEMPLATE.format(content="third forward")


def test_foreign_live_question_is_dropped_but_its_material_stays_as_background() -> None:
    """Another run owes the question; the forward is context the user gave everyone.

    Dropping foreign material left the agent blind to what the user had just
    forwarded (measured live 29.07): it answered "I see no forwarded
    messages" while the forwards sat in a sibling collection.
    """
    messages = [
        own_message("mine"),
        own_message("someone else's question", exchange_id=FOREIGN_EXCHANGE),
        material_message("someone else's forward", exchange_id=FOREIGN_EXCHANGE),
    ]

    rendered = render_branch(messages, OWN_EXCHANGE, frozenset({FOREIGN_EXCHANGE}))

    assert [m.content for m in rendered] == [
        TASK_NOTE_TEMPLATE.format(content="mine"),
        SHARED_MATERIAL_NOTE_TEMPLATE.format(content="someone else's forward"),
    ]


def test_foreign_non_live_exchange_messages_pass_through_unmarked() -> None:
    """A settled (answered/cancelled) foreign exchange is plain history, not dropped."""
    foreign = own_message("already answered elsewhere", exchange_id=FOREIGN_EXCHANGE)
    messages = [own_message("mine"), foreign]

    rendered = render_branch(messages, OWN_EXCHANGE, NO_LIVE_EXCHANGES)

    assert rendered[0].content == TASK_NOTE_TEMPLATE.format(content="mine")
    assert rendered[1].content == "already answered elsewhere"
    assert rendered[1] is foreign  # untouched, not even copied


def test_messages_without_an_exchange_or_non_user_role_pass_through_untouched() -> None:
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="sys prompt"),
        ChatMessage(role=MessageRole.ASSISTANT, content="an answer"),
        ChatMessage(role=MessageRole.USER, content="legacy row, no exchange"),
        ChatMessage(role=MessageRole.TOOL, content="tool output", tool_call_id="call-1"),
    ]

    rendered = render_branch(messages, OWN_EXCHANGE, NO_LIVE_EXCHANGES)

    assert len(rendered) == len(messages)
    for original, got in zip(messages, rendered, strict=True):
        assert got is original  # same object: never even copied


def test_no_own_exchange_never_marks_anything() -> None:
    """RUN/self-contained branches pass own_exchange_id=None: nothing is 'own'."""
    messages = [own_message("someone's question"), material_message("someone's forward")]

    rendered = render_branch(messages, None, NO_LIVE_EXCHANGES)

    assert [m.content for m in rendered] == ["someone's question", "someone's forward"]
    for original, got in zip(messages, rendered, strict=True):
        assert got is original
