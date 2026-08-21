"""Pagination cursor state and declared-total stop checks."""

from octoforge_core.net.collections.ingest import ParsedBody, dotted_get
from octoforge_core.net.spec_types import PaginationKind, PaginationSpec


class PageCursor:
    def __init__(self, spec: PaginationSpec) -> None:
        self._spec = spec
        self._value = "" if spec.kind is PaginationKind.CURSOR else str(spec.start)
        self._seen = {self._value}

    @property
    def value(self) -> str:
        return self._value

    def advance(self, parsed: ParsedBody) -> bool:
        if self._spec.kind is PaginationKind.PAGE:
            self._value = str(int(self._value) + 1)
            return True
        if self._spec.kind is PaginationKind.OFFSET:
            self._value = str(int(self._value) + len(parsed.records.payloads))
            return True
        found = dotted_get(parsed.document, self._spec.cursor_path or "")
        if found is None or isinstance(found, bool) or not isinstance(found, str | int):
            return False
        value = str(found)
        if not value or value in self._seen:
            return False
        self._seen.add(value)
        self._value = value
        return True


def reached_total(spec: PaginationSpec, parsed: ParsedBody, collected: int) -> bool:
    if spec.total_path is None:
        return False
    total = dotted_get(parsed.document, spec.total_path)
    return isinstance(total, int) and not isinstance(total, bool) and collected >= total
