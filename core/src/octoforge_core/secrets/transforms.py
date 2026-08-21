"""Pure transformations available to endpoint secret substitution."""

import base64
import hashlib

from octoforge_core.secrets.types import SecretTransform


def apply_transform(value: str, transform: SecretTransform | None) -> str:
    """Return the value as it must appear in the request."""
    match transform:
        case None:
            return value
        case SecretTransform.BASE64:
            return base64.b64encode(value.encode()).decode()
        case SecretTransform.BASE64URL:
            return base64.urlsafe_b64encode(value.encode()).decode()
        case SecretTransform.MD5_HEX:
            return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()
        case SecretTransform.SHA1_HEX:
            return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()
        case SecretTransform.SHA256_HEX:
            return hashlib.sha256(value.encode()).hexdigest()
