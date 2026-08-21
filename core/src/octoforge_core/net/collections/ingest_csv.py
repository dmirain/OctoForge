"""CSV parsing for collection ingestion."""

import csv
import io

from octoforge_core.net.collections.api import CollectionKind, NewRecords
from octoforge_core.net.collections.ingest_models import ParsedBody

MAX_RECORDS = 100_000
MIN_CSV_ROWS = 2


def parse_csv(body: str) -> ParsedBody | None:
    """Turn header-first CSV into records, retaining values as strings."""
    dialect = _dialect(body[:4096])
    rows = list(csv.reader(io.StringIO(body), dialect))
    if len(rows) < MIN_CSV_ROWS:
        return None
    header = [name.strip() or f"column_{index}" for index, name in enumerate(rows[0])]
    payloads = [
        {header[index]: cell for index, cell in enumerate(row) if index < len(header)}
        for row in rows[1 : MAX_RECORDS + 1]
    ]
    return ParsedBody(
        kind=CollectionKind.CSV,
        records=NewRecords(payloads=payloads),
        envelope={},
        record_truncated=len(rows) - 1 > MAX_RECORDS,
    )


def _dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel
