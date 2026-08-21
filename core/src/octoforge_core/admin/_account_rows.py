"""Map secret metadata rows without exposing ciphertext."""

from octoforge_core.admin.account_types import SecretOverview
from octoforge_core.secrets.api import DEFAULT_PLACEMENTS
from octoforge_core.secrets.models import SecretRow


def to_secret(row: SecretRow) -> SecretOverview:
    placements = (
        tuple(sorted(member.value for member in DEFAULT_PLACEMENTS))
        if row.placements is None
        else tuple(row.placements.split(","))
    )
    return SecretOverview(
        row.user_id,
        row.code,
        row.allowed_host,
        row.description,
        placements,
        row.transform,
        row.created_at,
        row.last_used_at,
    )
