"""Tests for the skill pre-search adapter over the instructions facade."""

from datetime import UTC, datetime

from octoforge_core.instructions.api import (
    Instruction,
    InstructionType,
    SearchHit,
)
from octoforge_core.instructions.presearch import (
    MAX_SKILLS,
    PER_QUERY_K,
    InstructionPresearch,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def make_hit(kind: InstructionType, title: str, score: float, content: str = "body") -> SearchHit:
    return SearchHit(
        instruction=Instruction(
            id=f"id-{title}",
            type=kind,
            title=title,
            content=content,
            tags=(),
            version=1,
            usage_count=0,
            success_count=0,
            created_at=CREATED_AT,
            updated_at=CREATED_AT,
        ),
        score=score,
    )


class FakeInstructionService:
    """InstructionService stub mapping queries to scripted hits."""

    def __init__(self, hits_by_query: dict[str, list[SearchHit]]) -> None:
        self._hits_by_query = hits_by_query
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, k: int) -> list[SearchHit]:
        self.calls.append((query, k))
        return self._hits_by_query.get(query, [])


async def test_run_searches_every_query_with_per_query_k() -> None:
    service = FakeInstructionService({})
    presearch = InstructionPresearch(service)

    assert await presearch.run(("q1", "q2")) is None
    assert service.calls == [("q1", PER_QUERY_K), ("q2", PER_QUERY_K)]


async def test_run_returns_none_when_nothing_found() -> None:
    service = FakeInstructionService({})

    assert await InstructionPresearch(service).run(("q",)) is None


async def test_skill_hits_go_in_with_full_content() -> None:
    content = "Scenario: do X.\n1. Step one.\n2. Step two."
    service = FakeInstructionService({"q": [make_hit(InstructionType.SKILL, "do_x", 0.9, content)]})

    note = await InstructionPresearch(service).run(("q",))

    assert note is not None
    assert note.startswith("Relevant scenarios for this run (follow them):")
    assert f"1. [skill] do_x\n{content}" in note


async def test_hits_are_deduplicated_by_title_keeping_the_best_score() -> None:
    service = FakeInstructionService(
        {
            "q1": [make_hit(InstructionType.SKILL, "do_x", 0.4)],
            "q2": [make_hit(InstructionType.SKILL, "do_x", 0.9)],
        }
    )

    note = await InstructionPresearch(service).run(("q1", "q2"))

    assert note is not None
    assert note.count("[skill] do_x") == 1


async def test_skills_are_capped_and_ordered_by_score() -> None:
    hits = [
        make_hit(InstructionType.SKILL, "s1", 0.4),
        make_hit(InstructionType.SKILL, "s2", 0.9),
        make_hit(InstructionType.SKILL, "s3", 0.7),
        make_hit(InstructionType.SKILL, "s4", 0.1),
    ]
    service = FakeInstructionService({"q": hits})

    note = await InstructionPresearch(service).run(("q",))

    assert note is not None
    assert note.count("[skill]") == MAX_SKILLS
    assert "[skill] s4" not in note
    assert note.index("[skill] s2") < note.index("[skill] s3")


async def test_knowledge_and_endpoints_become_one_line_references() -> None:
    service = FakeInstructionService(
        {
            "q": [
                make_hit(InstructionType.SKILL, "do_x", 0.9),
                make_hit(InstructionType.KNOWLEDGE, "api fact", 0.8),
                make_hit(InstructionType.ENDPOINT, "wttr_in_weather", 0.7),
            ]
        }
    )

    note = await InstructionPresearch(service).run(("q",))

    assert note is not None
    assert "- [knowledge] api fact" in note
    assert "- [endpoint] wttr_in_weather" in note
    # references are one-liners: no full content block for them
    assert note.count("\nbody") == 1  # only the skill's content


async def test_references_alone_still_produce_a_note() -> None:
    service = FakeInstructionService({"q": [make_hit(InstructionType.ENDPOINT, "ep", 0.8)]})

    note = await InstructionPresearch(service).run(("q",))

    assert note is not None
    assert "- [endpoint] ep" in note
