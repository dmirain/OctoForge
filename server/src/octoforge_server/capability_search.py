"""Database vector and lexical retrieval capability descriptions."""

from octoforge_core.composition import LexicalBackend
from octoforge_core.db.search_extensions import VECTOR

from octoforge_server.capability_model import Capability

LEXICAL_DETAIL = {
    LexicalBackend.POSTGRES: "pg_textsearch, BM25 over the russian_unaccent config",
    LexicalBackend.SQLITE: "SQLite FTS5, BM25 over trigrams (no russian stemming)",
    LexicalBackend.NONE: "unavailable here - recall is embeddings only",
}


def search_capabilities(
    search_extensions: frozenset[str],
    lexical_backend: LexicalBackend,
) -> tuple[Capability, ...]:
    vector = VECTOR in search_extensions
    return (
        Capability(
            "vector search",
            vector,
            (
                "pgvector ranks in the database"
                if vector
                else "no pgvector - the visible table is ranked in process"
            ),
        ),
        Capability(
            "lexical search",
            lexical_backend is not LexicalBackend.NONE,
            LEXICAL_DETAIL[lexical_backend],
        ),
    )
