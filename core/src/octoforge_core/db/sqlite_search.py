"""FTS5 mirror descriptions and safe user-query rendering."""

from dataclasses import dataclass

MIN_TOKEN_LENGTH = 3
TITLE_WEIGHT = 10.0
BODY_WEIGHT = 1.0
TOKENIZER = "trigram remove_diacritics 1"
FTS_SUFFIX = "_fts"


@dataclass(frozen=True, slots=True)
class FtsTable:
    """One FTS5 mirror and the source columns it indexes."""

    name: str
    source: str
    columns: tuple[str, ...]


FTS_TABLES = (
    FtsTable("instructions_fts", "instructions", ("title", "content")),
    FtsTable("messages_fts", "messages", ("content",)),
    FtsTable("datasets_fts", "datasets", ("description",)),
)


def match_expression(query: str) -> str | None:
    """Render safe, any-term FTS5 syntax, or None when no trigram remains."""
    tokens = [token for token in query.split() if len(token.strip('"')) >= MIN_TOKEN_LENGTH]
    if not tokens:
        return None
    quoted = ['"' + token.replace('"', '""') + '"' for token in tokens]
    return " OR ".join(quoted)


def rank_expression(table: FtsTable) -> str:
    """Render bm25() with title weighting when a mirror has a title."""
    if len(table.columns) == 1:
        return f"bm25({table.name})"
    weights = ", ".join(
        str(TITLE_WEIGHT if column == "title" else BODY_WEIGHT) for column in table.columns
    )
    return f"bm25({table.name}, {weights})"
