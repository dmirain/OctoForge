"""Model-facing rendering of collection passports."""

import json

from octoforge_core.net.collections.api import CollectionPassport
from octoforge_core.net.collections.schema_infer import render
from octoforge_core.time import utc_now

MAX_ENVELOPE_CHARS = 1000
KILOBYTE = 1024
MEGABYTE = KILOBYTE * KILOBYTE
PASSPORT_TEMPLATE = (
    "[collection {ref}] kind={kind} · source={source} · {records} records · "
    "{size} · expires in {minutes} min{truncated}\n"
    "record schema: {schema}\n"
    "{envelope}"
    "The body is NOT in this message — query the data with collection_query: "
    "ops get/pluck/count/sum/avg/min/max/distinct, filters, group_by; "
    "collection_get re-reads this passport."
)
TRUNCATED_NOTE = " · SOURCE CUT at the wire limit: counts reflect what arrived"


def render_passport(passport: CollectionPassport) -> str:
    """Render the model-facing form of a collection passport."""
    minutes = max(0, int((passport.expires_at - utc_now()).total_seconds() // 60))
    return PASSPORT_TEMPLATE.format(
        ref=passport.ref,
        kind=passport.kind.value,
        source=passport.source or "-",
        records=passport.record_count,
        size=_human_size(passport.byte_size),
        minutes=minutes,
        truncated=TRUNCATED_NOTE if passport.truncated else "",
        schema=render(passport.schema),
        envelope=_render_envelope(passport),
    )


def _render_envelope(passport: CollectionPassport) -> str:
    if not passport.envelope:
        return ""
    rendered = json.dumps(passport.envelope, ensure_ascii=False)
    if len(rendered) > MAX_ENVELOPE_CHARS:
        rendered = rendered[:MAX_ENVELOPE_CHARS] + "…"
    return f"envelope: {rendered}\n"


def _human_size(chars: int) -> str:
    if chars >= MEGABYTE:
        return f"{chars / MEGABYTE:.1f} MB"
    if chars >= KILOBYTE:
        return f"{chars / KILOBYTE:.1f} KB"
    return f"{chars} chars"
