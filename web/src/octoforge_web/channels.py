"""The surfaces a dialog can belong to.

A dialog is identified by `(user_id, channel)`, so the channel decides *which*
conversation a request touches. Core treats it as an opaque string — the
surfaces are a property of this deployment, not of the library — which is why
the list of real ones lives here, at the edge that serves them.
"""

from octoforge_web.telegram.client import TELEGRAM_CHANNEL

WEB_CHANNEL = "web"

#: Every channel this application serves. A request naming anything else is
#: rejected rather than quietly given a dialog of its own: a typo would strand
#: a user's messages in a conversation nobody reads.
KNOWN_CHANNELS = frozenset({WEB_CHANNEL, TELEGRAM_CHANNEL})
