"""Execute one prepared HTTP call with capped reads and secret scrubbing."""

import httpx

from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external_types import ExternalPage, PreparedHttpCall
from octoforge_core.net.external_values import scrub


class ExternalHttpRunner:
    def __init__(self, http: httpx.AsyncClient, timeout_seconds: float, wire_limit: int) -> None:
        self._http = http
        self._timeout = timeout_seconds
        self._wire_limit = wire_limit

    async def run(self, call: PreparedHttpCall) -> ExternalPage:
        try:
            async with self._http.stream(
                call.method,
                call.url,
                headers=call.headers,
                content=call.body,
                follow_redirects=False,
                timeout=self._timeout,
            ) as response:
                raw, truncated = await read_capped_text(response, self._wire_limit)
                status = response.status_code
                content_type = response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            raise ExternalCallError(
                f"external call failed: {scrub(str(exc), call.secrets)}"
            ) from exc
        return ExternalPage(
            status,
            scrub(raw, call.secrets),
            content_type,
            truncated,
            bool(call.secrets),
        )


async def read_capped_text(response: httpx.Response, limit: int) -> tuple[str, bool]:
    chunks: list[bytes] = []
    size = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            truncated = True
            break
    raw = b"".join(chunks)[:limit]
    return raw.decode(response.encoding or "utf-8", errors="replace"), truncated
