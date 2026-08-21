"""Public pure ranking functions used by instruction search and its tests."""

from octoforge_core.instructions._cosine_ranking import EXACT_TITLE_BOOST, rank
from octoforge_core.instructions._hybrid_ranking import (
    RRF_SMOOTHING,
    fuse,
    fuse_rankings,
    rerank,
)

__all__ = [
    "EXACT_TITLE_BOOST",
    "RRF_SMOOTHING",
    "fuse",
    "fuse_rankings",
    "rank",
    "rerank",
]
