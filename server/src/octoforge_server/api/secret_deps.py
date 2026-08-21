"""Request-scoped collaborators for self-service secret endpoints."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from octoforge_core.identity.api import IdentityStore
from octoforge_core.secrets.api import SecretStore

from octoforge_server.deps import get_identity_store, get_secret_links, get_secret_store
from octoforge_server.secret_links import SecretLinkService


@dataclass(frozen=True, slots=True)
class SecretServices:
    store: Annotated[SecretStore | None, Depends(get_secret_store)]
    links: Annotated[SecretLinkService, Depends(get_secret_links)]
    identities: Annotated[IdentityStore, Depends(get_identity_store)]


SecretServicesDep = Annotated[SecretServices, Depends()]
