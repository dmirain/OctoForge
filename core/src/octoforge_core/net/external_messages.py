"""Limits and user-facing diagnostics for stored endpoint execution."""

MAX_BODY_CHARS = 8000
MAX_BODY_BYTES = 2 * 1024 * 1024
TRUNCATED_SUFFIX = "\n...[truncated]"
USER_ID_PLACEHOLDER = "{user_id}"
SECRET_SCRUBBED = "[secret]"
SECRETS_DISABLED_MESSAGE = (
    "this endpoint requires a per-user secret, but secrets are not configured "
    "on this installation (OF_SECRETS_KEY is not set)"
)
PARAMS_DISABLED_MESSAGE = (
    "this endpoint uses per-user params ({{user.*}}), but user params are not "
    "wired on this installation"
)
SECRET_MISSING_TEMPLATE = (
    "secret '{code}' is not set for this user (needed for host '{host}'): mint "
    "them a pre-filled one-time form link with the secret_link tool - pass this "
    "code, this host and a clear description. Without the tool they can run "
    "/secrets in Telegram and fill the form by hand"
)
PARAM_MISSING_TEMPLATE = (
    "user param(s) not set for this user: {codes}. An operator sets them in the "
    "admin console; tell the user which value is needed and what for"
)
PLACEMENT_BLOCKED_TEMPLATE = (
    "secret '{code}' may not be substituted into the {part} of a request "
    "(it allows: {allowed}); the user can extend its placements in the secrets form"
)
UNAUTHENTICATED_STATUSES = frozenset({401, 403})
NO_CREDENTIAL_HINT = (
    "\n\n[octoforge] This request carried NO credential: the endpoint record "
    "references no secret, so nothing was attached. Do not guess at the secret's "
    "value or encoding - fix the record. Declare the secret as "
    'auth: {"secret": "<code>"} or reference it as {secret.<code>} in a header '
    "template, and check secret_list for the codes this user actually has."
)
