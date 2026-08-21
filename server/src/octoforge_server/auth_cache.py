"""Bounded failed-attempt tracking and successful credential caching."""

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_FAILED_ATTEMPTS = 5
ATTEMPT_COOLDOWN_SECONDS = 60.0
MAX_TRACKED_CLIENTS = 10_000
CREDENTIAL_CACHE_SECONDS = 60.0
MAX_CACHED_CREDENTIALS = 32


@dataclass(slots=True)
class AttemptLimiter:
    max_failures: int = MAX_FAILED_ATTEMPTS
    cooldown_seconds: float = ATTEMPT_COOLDOWN_SECONDS
    max_clients: int = MAX_TRACKED_CLIENTS
    _failures: OrderedDict[str, tuple[int, float]] = field(default_factory=OrderedDict)

    def blocked(self, client: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        entry = self._failures.get(client)
        if entry is None:
            return False
        count, last = entry
        if moment - last >= self.cooldown_seconds:
            del self._failures[client]
            return False
        return count >= self.max_failures

    def record_failure(self, client: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        count, last = self._failures.get(client, (0, moment))
        if moment - last >= self.cooldown_seconds:
            count = 0
        self._failures[client] = (count + 1, moment)
        self._failures.move_to_end(client)
        while len(self._failures) > self.max_clients:
            self._failures.popitem(last=False)

    def record_success(self, client: str) -> None:
        self._failures.pop(client, None)


@dataclass(slots=True)
class CredentialCache:
    ttl_seconds: float = CREDENTIAL_CACHE_SECONDS
    max_entries: int = MAX_CACHED_CREDENTIALS
    _entries: OrderedDict[str, float] = field(default_factory=OrderedDict)

    def valid(self, header: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        key = credential_key(header)
        expires_at = self._entries.get(key)
        if expires_at is None:
            return False
        if expires_at <= moment:
            del self._entries[key]
            return False
        return True

    def remember(self, header: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        self._entries[credential_key(header)] = moment + self.ttl_seconds
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


def credential_key(header: str) -> str:
    return hashlib.sha256(header.encode()).hexdigest()
