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

`auth` may instead declare a per-user secret:

```json
{"auth": {"secret": "gmail_token", "header": "Authorization", "format": "Bearer {value}"}}
```

The model only ever sees the *code* (`gmail_token`). The executor resolves the value from the encrypted
store at request time and injects it as a header. See [secrets.md](secrets.md).

### Executing a call

`ExternalCallExecutor` does, in order:

1. read the record through the instruction service (owner-scoped: another user's private endpoint is
   invisible);
2. parse the JSON contract (`ToolSpecError` when malformed);
3. validate the given parameters against `params_schema` — required present, unknown rejected. **A
   validation error returns the declared contract**, so a blind call can self-correct in one step;
4. render the URL template with `urllib.parse.quote`-escaped values;
5. check the URL with the SSRF guard;
6. inject infrastructure auth if the origin matches `OF_EXTERNAL_CALL_AUTH_WHITELIST`, and resolve the
   per-user secret if the record declares one;
7. perform the request with `follow_redirects=False`;
8. truncate the response body to 8000 characters and scrub any echo of a secret value before returning
   it.

Substitution of secret values happens **only** in the record's own template — never in
agent-supplied parameter values, which would hand a prompt-injected agent an exfiltration channel — and
only into headers, because a secret in a query string leaks into URLs, proxies and logs.

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
- **Parameters are validated before the URL is rendered**, and unknown parameters are refused.
- **A parameter-validation error carries the contract** back to the model.
- **Secrets are substituted only into headers, only from the record's template.**
- **Secret values are scrubbed from responses** before they reach the context or the logs.
- **Redirects are never followed.**
- **Every outbound URL goes through the guard**, except explicitly allowlisted origins.
- **Response bodies are truncated** so one call cannot flood the context: 8000 characters for
  `external_call`, 4000 for `http_request`.

## Configuration

| Variable | Effect |
|---|---|
| `OF_SELF_BASE_URL` | The one allowlisted origin: this application's own API |
| `OF_EXTERNAL_CALL_AUTH_WHITELIST` | JSON list of `{base_url_prefix, header_name, header_value}`; `header_value` may contain `{user_id}` |
| `OF_SECRETS_KEY` | Without it, endpoints declaring `auth.secret` fail with a clear message |
| `OF_HTTP_REQUEST_ALLOWLIST` | Origins `http_request` may call; empty means the open web |

## Failure modes

| Situation | Outcome |
|---|---|
| URL resolves to a private or metadata address | `SsrfBlockedError`; the model is told the target is not allowed |
| `http_request` targets an origin outside the allowlist | `EgressBlockedError` naming the permitted origins and pointing at `recall(type=endpoint)` |
| Endpoint record content is not valid JSON | `ToolSpecError`, reported to the model |
| Missing or unknown parameter | Validation error including the declared contract |
| Secret not set for this user | Message telling the agent to ask the user to add it via `/secrets` |
| Secret asked for a host it is not bound to | `SecretHostMismatchError` — the value is not sent |
| Secrets not configured at all | Explicit "secrets are not configured on this installation" |
| Server redirects | Not followed; the redirect response is what the model sees |
| Huge response | Truncated with a marker (8000 characters via `external_call`, 4000 via `http_request`) |
| DNS rebinding between check and connect | Not currently prevented — see the gap above |

## Code anchors

- `core/src/octoforge_core/net/guard.py` — `SsrfGuard`, origin allowlisting, the documented TOCTOU gap
- `core/src/octoforge_core/net/tool_spec.py` — the endpoint document format and its parsing
- `core/src/octoforge_core/net/external.py` — `ExternalCallExecutor`, auth injection, scrubbing
- `core/src/octoforge_core/net/tools.py` — `http_request`, `endpoint_get`, `external_call`
- `core/src/octoforge_core/net/errors.py` — `SsrfBlockedError`, `ExternalCallError`, `ToolSpecError`
- `core/tests/test_ssrf_guard.py`, `core/tests/test_external_call.py`,
  `core/tests/test_http_request_tool.py`
