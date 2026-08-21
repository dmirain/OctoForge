"""Open paths, service-credential scope and browser CSRF checks."""

from fastapi import Request

OPEN_PATHS = frozenset({"/health", "/health/ready", "/secrets.html"})
OPEN_PREFIXES = ("/api/secrets/",)
SERVICE_PREFIXES = ("/api/dialog/", "/api/media/", "/api/identity/profile")
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FETCH_SITE_HEADER = "sec-fetch-site"
ORIGIN_HEADER = "origin"
SAME_SITE_VALUES = frozenset({"same-origin", "none"})
CROSS_SITE_MESSAGE = "cross-site state-changing requests are refused"


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def allows_service_credential(path: str) -> bool:
    return path.startswith(SERVICE_PREFIXES)


def is_cross_site_mutation(request: Request) -> bool:
    if request.method.upper() not in MUTATING_METHODS:
        return False
    fetch_site = request.headers.get(FETCH_SITE_HEADER, "").lower()
    if fetch_site:
        return fetch_site not in SAME_SITE_VALUES
    origin = request.headers.get(ORIGIN_HEADER)
    if not origin:
        return False
    return origin.rstrip("/").lower() != _own_origin(request)


def _own_origin(request: Request) -> str:
    url = request.url
    host = request.headers.get("host") or url.netloc
    return f"{url.scheme}://{host}".rstrip("/").lower()
