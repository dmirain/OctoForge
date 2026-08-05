# Endpoints and outbound HTTP

How the agent reaches other systems: a stored contract it executes, a raw request when there is no
contract yet, and one guard in front of both.

## How it works

Three tools, in the order the agent is told to prefer them:

| Tool | Purpose |
|---|---|
| `endpoint_get(name)` | Exact-name lookup of a stored endpoint's contract — *late binding*. A skill mentions an endpoint by name; this turns the name into the method, URL template, parameter schema and auth declaration the model needs to call it correctly |
| `external_call(name, params?)` | Execute a stored endpoint record with validated parameters |
| `http_request(method, url, headers?, body?)` | A raw outbound request, for what has no stored contract. Confined to `OF_HTTP_REQUEST_ALLOWLIST` when that names any origins |

Discovery is `recall`'s job (`type=endpoint`); `endpoint_get` is a lookup, not a search.

### The endpoint record

An endpoint is an instruction record whose content is a small JSON document:

```json
{"method": "GET",
 "url_template": "https://wttr.in/{city}?format=j2",
 "params_schema": {"city": {"type": "string", "required": true}},
 "auth": "none"}
```

The method may be any of the classic verbs or the WebDAV family (`PROPFIND`, `PROPPATCH`, `REPORT`,
`MKCOL`, `MKCALENDAR`, `COPY`, `MOVE`, plus `HEAD`/`OPTIONS`) — what CalDAV/CardDAV servers speak.
`LOCK`/`UNLOCK` are deliberately not allowed: an agent cannot manage a lock's lifetime and would
leave servers with dangling locks. A record may also declare a request body template and headers,
which those protocols need:

```json
{"method": "REPORT",
 "url_template": "https://cal.example.com/dav/{calendar}/",
 "body_template": "<c:calendar-query>...{start}...</c:calendar-query>",
 "headers": {"Depth": "1", "Content-Type": "application/xml"}}
```

### The template language

`url_template`, `body_template` and every header value speak one placeholder language with three
namespaces:

| Placeholder | Filled by | Source |
|---|---|---|
| `{city}` | the model | must be declared (and `required`) in `params_schema` |
| `{user.timezone}` | the executor | the calling user's stored params, set by the operator in the console |
| `{secret.gmail_token}` | the executor | the calling user's encrypted secrets — see [secrets.md](secrets.md) |

The model sees the record and therefore every placeholder *name*; the `user.` and `secret.` values
are substituted server-side after the model's output is fixed and never enter a prompt. That is the
whole point of the namespaces: a timezone, an account id or a calendar path is deterministic per
user, so it lives in the record's template instead of the model's context — and a credential never
enters the context at all.

A declared parameter has a **type**, which is what it may carry and how it is escaped:

| `type` | Carries | Escaping |
|---|---|---|
| `string` | one path segment (the default) | everything reserved, separators included |
| `path` | several segments — a discovery href such as an account id followed by `calendars` | each segment, the slashes between them survive |
| `host` | a hostname, and only one the record's own `hosts` allowlist covers | none; the value is validated, not escaped |

`path` exists because escaping a separator is not a cosmetic detail. A `string` renders the href
`<account>/principal/` as `<account>%2Fprincipal%2F`, which iCloud answers with 401 — that was every
CalDAV call after discovery until this type existed.

`host` is what lets a contract follow a service that shards across sibling hosts — CalDAV discovery
answers on `caldav.icloud.com` and then hands out `p124-caldav.icloud.com` — without hard-coding one
user's shard into the record:

```json
{"url_template": "https://{host}/{calendar_home_path}",
 "params_schema": {"host": {"type": "host", "required": true, "hosts": ["*.icloud.com"]},
                   "calendar_home_path": {"type": "path", "required": true}}}
```

The allowlist is written by the record, never by the caller, and uses the same grammar as a secret's
host binding (an exact hostname or a one-level `*.` pattern). **Only a `host` param may stand where
the URL's host does** — a `string` or `path` there would let the caller name the destination
outright. A `{user.*}` value may: an operator set it, and an operator can write any URL into the
record anyway. A `{secret.*}` may not, ever.

Substitution rules by destination: URL values are escaped per the table above; header values are
substituted verbatim but the rendered header must be printable ASCII (a model param carrying a
newline fails the call — the header-injection guard); body values go in **verbatim** (URL-escaping
would corrupt an XML or JSON payload; format-appropriate escaping is the record author's concern).
Format specs and conversions (`{x:>10}`, `{x!r}`) are rejected at parse time. `params_schema` keys
may not contain a dot — dotted names are the namespaces'.

Secrets additionally obey their own `placements` (headers by default; `url` and `body` are opt-in
per secret) and can never form the URL scheme or host. The `auth` object remains as sugar for the
one-header case and expands into a header template:

```json
{"auth": {"secret": "gmail_token", "header": "Authorization", "format": "Bearer {value}"}}
```

is exactly `"headers": {"Authorization": "Bearer {secret.gmail_token}"}`.

### Executing a call

`ExternalCallExecutor` does, in order:

1. read the record through the instruction service (owner-scoped: another user's private endpoint is
   invisible);
2. sniff the content's `kind` discriminator — a record carrying one belongs to another protocol's
   delegate (`kind: "mcp"` hands off to the MCP executor with params passed through as structured
   values; see [mcp.md](mcp.md)), while the classic contract below stays strings-only;
3. parse the JSON contract (`ToolSpecError` when malformed);
4. validate the given parameters against `params_schema` — required present, unknown rejected,
   values must be strings. **A validation error returns the declared contract**, so a blind call can
   self-correct in one step;
5. load the referenced `{user.*}` values — a missing one fails the call with the code and where to
   set it;
6. render the URL from model params and user values (quote-escaped); `{secret.*}` refs ride as
   unguessable per-call sentinels for now;
7. check the URL with the SSRF guard;
8. resolve every referenced secret for the host that URL names — the host binding and the
   placements are enforced here, and the secret value has had no say in what the host is — then
   substitute the sentinels;
9. render headers (header-safety enforced) and the body; merge headers lowest-precedence first:
   record headers without secret refs, then infrastructure auth for origins matching
   `OF_EXTERNAL_CALL_AUTH_WHITELIST`, then the record's secret-bearing headers — so a plain record
   header never shadows a credential;
10. perform the request with `follow_redirects=False`;
11. truncate the response body to 8000 characters and scrub any echo of every resolved secret —
    both the sent and the stored form — before returning it.

Substitution of `user.*` and `secret.*` values happens **only** in the record's own templates —
never in agent-supplied parameter values, which would hand a prompt-injected agent an exfiltration
channel.

### The SSRF guard

`SsrfGuard.check(url)` resolves the hostname and rejects the call if **any** resolved address is
private, loopback, link-local (which covers the cloud metadata address `169.254.169.254`), multicast,
reserved, unspecified, or otherwise not globally routable (the CGNAT range `100.64.0.0/10` passes all six
named predicates, so routability is checked explicitly).

Only `http` and `https` schemes are allowed. **Redirects are never followed** by either tool: the guard
validated one URL, and a redirect would re-enter unchecked address space.

`allowed_prefixes` bypass the check by *origin* — scheme, host and port, parsed rather than string-matched,
so `http://allowed@169.254.169.254/` does not sneak through and an allowlisted origin's path is
irrelevant. This exists for exactly one purpose: the application's own base URL
(`OF_SELF_BASE_URL`), so stored endpoints can call this installation's own API. It is not for
user-controlled targets.

**Known gap:** the address is checked at validation time and resolved again by the HTTP client at connect
time, so an attacker-controlled DNS record can answer differently between the two (TOCTOU / DNS
rebinding). Closing it means connecting by resolved IP with an explicit `Host` header. Tracked in
[../limitations.md](../limitations.md).

## Invariants

- **Endpoint records are owner-scoped.** A private endpoint of another user cannot be executed.
- **Unknown document fields are refused**, and a string `auth` may only be `"none"`. An ignored
  field is how a record ends up sending no credential and no body while looking authenticated —
  three production records had drifted into `{"auth": "bearer", "secret_key": "…"}`, which attaches
  nothing. `notes` and `description` stay allowed as free-form documentation.
- **A 401/403 answered to a call that carried no secret says so**, so the model fixes the record
  instead of guessing at the credential's value or encoding.
- **Parameters are validated before anything is rendered**, and unknown parameters are refused.
- **A parameter-validation error carries the contract** back to the model.
- **`user.*` and `secret.*` values are substituted only from the record's templates** — never from
  agent-supplied parameter values.
- **Only a `host` parameter may occupy the URL's host**, and only within the allowlist its record
  declares — so a caller picks from what the record permits, it never names a destination.
- **A `path` parameter may not walk up (`..`) or carry a scheme, query or fragment**, so it stays a
  path rather than becoming a URL.
- **Secrets go only where their placements allow** (headers by default), never into the URL host,
  and are resolved only after the destination has been rendered and checked without them.
- **Rendered headers must be header-safe** — a value carrying control characters or non-ASCII fails
  the call instead of reaching the wire.
- **A plain record header cannot shadow a credential header** (merge order above).
- **Secret values are scrubbed from responses** — sent and stored forms both — before they reach the
  context or the logs.
- **Redirects are never followed.**
- **Every outbound URL goes through the guard**, except explicitly allowlisted origins.
- **Response bodies are truncated** so one call cannot flood the context: 8000 characters for
  `external_call`, 4000 for `http_request`.

## Configuration

| Variable | Effect |
|---|---|
| `OF_SELF_BASE_URL` | The one allowlisted origin: this application's own API |
| `OF_EXTERNAL_CALL_AUTH_WHITELIST` | JSON list of `{base_url_prefix, header_name, header_value}`; `header_value` may contain `{user_id}` |
| `OF_SECRETS_KEY` | Without it, endpoints referencing `{secret.*}` fail with a clear message |
| `OF_HTTP_REQUEST_ALLOWLIST` | Origins `http_request` may call; empty means the open web |

## Failure modes

| Situation | Outcome |
|---|---|
| URL resolves to a private or metadata address | `SsrfBlockedError`; the model is told the target is not allowed |
| `http_request` targets an origin outside the allowlist | `EgressBlockedError` naming the permitted origins and pointing at `recall(type=endpoint)` |
| Endpoint record content is not valid JSON | `ToolSpecError`, reported to the model |
| Missing or unknown parameter | Validation error including the declared contract |
| Template references an unknown namespace or a dotted param name | `ToolSpecError` at parse time |
| A `string`/`path` param placed where the URL's host stands | `ToolSpecError` naming the rule |
| A `host` param without a `hosts` allowlist, or `hosts` on another type | `ToolSpecError` at parse time |
| A host value outside the record's allowlist | The call fails naming the allowed patterns; nothing is sent |
| Document carries an invented field (`secret_key`, `body`, …) | `ToolSpecError` naming the allowed fields and how a secret is actually declared |
| `auth` is a scheme word (`"basic"`, `"bearer"`) | `ToolSpecError`: it promises a credential and attaches none |
| Upstream answers 401/403 and no secret was attached | The body carries an explicit "this request carried NO credential" note |
| `{user.*}` value not set for this user | Message naming the code(s); an operator sets them in the console |
| Secret not set for this user | Message with the code and host, telling the agent to mint a `secret_link` |
| Secret asked for a host it is not bound to | `SecretHostMismatchError` — the value is not sent |
| Secret referenced where its placements forbid | The call fails naming the part; nothing is sent |
| Secrets not configured at all | Explicit "secrets are not configured on this installation" |
| Server redirects | Not followed; the redirect response is what the model sees |
| Huge response | Truncated with a marker (8000 characters via `external_call`, 4000 via `http_request`) |
| DNS rebinding between check and connect | Not currently prevented — see the gap above |

## Code anchors

- `core/src/octoforge_core/net/guard.py` — `SsrfGuard`, origin allowlisting, the documented TOCTOU gap
- `core/src/octoforge_core/net/tool_spec.py` — the endpoint document format, the template language
  and its parsing
- `core/src/octoforge_core/net/external.py` — `ExternalCallExecutor`, substitution order, scrubbing
- `core/src/octoforge_core/net/tools.py` — `http_request`, `endpoint_get`, `external_call`
- `core/src/octoforge_core/net/errors.py` — `SsrfBlockedError`, `ExternalCallError`, `ToolSpecError`
- `core/src/octoforge_core/params/api.py`, `core/src/octoforge_core/params/store.py` — the per-user
  params behind `{user.*}`
- `core/tests/test_ssrf_guard.py`, `core/tests/test_external_call.py`,
  `core/tests/test_http_request_tool.py`, `core/tests/test_user_params_store.py`
