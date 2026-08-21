"""Validation, encryption and decryption of secret values."""

from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from octoforge_core.secrets.policy import (
    normalize_code,
    normalize_description,
    normalize_host,
    normalize_placements,
    normalize_transform,
)
from octoforge_core.secrets.types import (
    InvalidSecretError,
    SecretPlacement,
    SecretTransform,
    SecretWrite,
)

MAX_VALUE_CHARS = 8192


class SecretDecryptionError(Exception):
    """Internal signal that persisted ciphertext cannot be opened."""


@dataclass(frozen=True, slots=True)
class PreparedSecret:
    """Validated metadata and encrypted value ready for persistence."""

    user_id: str
    code: str
    ciphertext: str
    allowed_host: str
    description: str
    placements: frozenset[SecretPlacement]
    transform: SecretTransform | None


class SecretValueCipher:
    """Validate writes and keep plaintext handling behind one narrow interface."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key)

    def prepare(self, request: SecretWrite) -> PreparedSecret:
        if not request.value or len(request.value) > MAX_VALUE_CHARS:
            raise InvalidSecretError(f"secret value must be 1..{MAX_VALUE_CHARS} characters")
        if not all(" " <= char <= "~" for char in request.value):
            raise InvalidSecretError(
                "secret value must be printable ASCII (it is sent inside an HTTP request)"
            )
        return PreparedSecret(
            request.user_id,
            normalize_code(request.code),
            self._fernet.encrypt(request.value.encode()).decode(),
            normalize_host(request.allowed_host),
            normalize_description(request.description),
            normalize_placements(request.placements),
            normalize_transform(request.transform),
        )

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise SecretDecryptionError from exc
