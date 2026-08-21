"""Rate-limited HTTP Basic gate for operator and surface credentials."""

import asyncio
import hmac
import logging
from dataclasses import dataclass, field
from http import HTTPStatus

from fastapi import HTTPException, Request

from octoforge_server.auth_basic import (
    BASIC_PREFIX,
    UNKNOWN_CLIENT,
    WWW_AUTHENTICATE,
    client_address,
    decode_basic,
    unauthorized,
)
from octoforge_server.auth_cache import MAX_FAILED_ATTEMPTS, AttemptLimiter, CredentialCache
from octoforge_server.auth_request_policy import (
    CROSS_SITE_MESSAGE,
    allows_service_credential,
    is_cross_site_mutation,
    is_open_path,
)
from octoforge_server.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

MISCONFIGURED_MESSAGE = "admin credentials are not configured"
TOO_MANY_ATTEMPTS_MESSAGE = "too many failed attempts; try again later"

__all__ = [
    "CROSS_SITE_MESSAGE",
    "MAX_FAILED_ATTEMPTS",
    "UNKNOWN_CLIENT",
    "AttemptLimiter",
    "AuthGate",
    "CredentialCache",
    "allows_service_credential",
    "hash_password",
    "is_cross_site_mutation",
    "is_open_path",
    "verify_password",
]


@dataclass(frozen=True, slots=True)
class AuthGate:
    username: str
    password_hash: str
    service_username: str = ""
    service_password_hash: str = ""
    limiter: AttemptLimiter = field(default_factory=AttemptLimiter)
    cache: CredentialCache = field(default_factory=CredentialCache)
    service_cache: CredentialCache = field(default_factory=CredentialCache)

    async def authenticate(self, request: Request, service_allowed: bool = False) -> None:
        if not self.username or not self.password_hash:
            raise HTTPException(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                detail=MISCONFIGURED_MESSAGE,
            )
        header = request.headers.get("authorization", "")
        if not header.lower().startswith(BASIC_PREFIX):
            raise unauthorized()
        if self._cached(header, service_allowed):
            return
        client = client_address(request)
        self._enforce_attempt_budget(client)
        candidate = decode_basic(header)
        if candidate is None:
            self.limiter.record_failure(client)
            raise unauthorized()
        valid, as_service, candidate_user = await self._verify(candidate, service_allowed)
        if not valid:
            self.limiter.record_failure(client)
            logger.warning("failed login for %r from %s", candidate_user, client)
            raise unauthorized()
        self.limiter.record_success(client)
        (self.service_cache if as_service else self.cache).remember(header)

    async def _verify(
        self,
        candidate: tuple[str, str],
        service_allowed: bool,
    ) -> tuple[bool, bool, str]:
        candidate_user, candidate_password = candidate
        as_service = (
            service_allowed
            and self._service_configured()
            and hmac.compare_digest(candidate_user, self.service_username)
        )
        expected = self.service_password_hash if as_service else self.password_hash
        user_ok = as_service or hmac.compare_digest(candidate_user, self.username)
        password_ok = await asyncio.to_thread(verify_password, candidate_password, expected)
        return user_ok and password_ok, as_service, candidate_user

    def _service_configured(self) -> bool:
        return bool(self.service_username and self.service_password_hash)

    def _cached(self, header: str, service_allowed: bool) -> bool:
        if self.cache.valid(header):
            return True
        return service_allowed and self._service_configured() and self.service_cache.valid(header)

    def _enforce_attempt_budget(self, client: str) -> None:
        if not self.limiter.blocked(client):
            return
        logger.warning("admin login refused (cooldown) from %s", client)
        raise HTTPException(
            status_code=HTTPStatus.TOO_MANY_REQUESTS,
            detail=TOO_MANY_ATTEMPTS_MESSAGE,
            headers=dict(WWW_AUTHENTICATE),
        )


def _client(request: Request) -> str:
    return client_address(request)
