# Teaching it without writing code

The three things that change what the agent can do without touching Python: a **skill** (how to do
something), a **knowledge** record (a fact), and an **endpoint** record (a system it can call). All of them
are rows the agent finds by meaning at request time.

## Skills

A skill is a scenario in plain text, retrieved by `recall`. Good ones read like an internal runbook: what
this is for, the steps in order, the tools by name, and what to do when something is missing.

```
Scenario: onboard a new contractor.
1. Ask for the full name, the start date and the team, if they were not given.
2. Look up the team lead: external_call with name 'staff_directory', params {"team": "<team>"}.
3. Create the checklist: data_put into the dataset 'onboarding' with fields
   name, start_date, team, lead, status="pending".
4. Confirm to the user what was recorded and who the lead is.
If the directory call fails, say so and record the entry without the lead
rather than inventing one.
```

What separates a skill that works from one that does not:

- **Name the tools and the record names.** "Look it up" is a wish; `external_call with name 'staff_directory'`
  is an instruction.
- **Say what to do on failure.** Otherwise the model improvises, and improvisation is where invented data
  comes from.
- **Keep it about doing, not about being.** Tone and house style belong in the system prompt, not in every
  skill.
- **One scenario per record.** Two loosely related procedures in one record rank worse for both.
- **Write triggers in the language your users type.** Ranking is embedding-based: a Russian phrasing does not
  reliably find an English scenario. Either write the record in that language or append the trigger phrases
  to it.

Save with `instruction_save` — ask the agent to save it, or write it yourself through the API. New records are
**private to their author**; an operator publishes what should be shared (see below).

## Knowledge

Same store, type `knowledge`: a fact, a policy, a piece of reference material worth remembering
installation-wide ("our fiscal year starts in April", "escalation path for production incidents").

The line between knowledge and memory is who it is about: something about *one person* is a
[memory](../reference/memory.md); something true for everyone is knowledge.

## Endpoint records

An endpoint record makes a system callable. Its content is a small JSON document:

```json
{"method": "GET",
 "url_template": "https://directory.internal/api/teams/{team}/lead",
 "params_schema": {"team": {"type": "string", "required": true}},
 "auth": {"secret": "directory_token", "header": "Authorization", "format": "Bearer {value}"}}
```

- `url_template` — placeholders are filled from validated parameters and URL-escaped.
- `body_template` (optional) — a request body with the same placeholders, filled **verbatim** (no
  URL-escaping; escape for the body's own format yourself).
- `headers` (optional) — headers the call carries (e.g. `Depth`, `Content-Type` for WebDAV/CalDAV);
  their values are templates too. A credential header from `auth` or the installation whitelist
  always wins a name collision with a plain record header.
- `params_schema` — declared parameters, `required` per parameter. Unknown parameters are refused, and a
  validation error hands the model this contract back, so a wrong call corrects itself in one step.
- `auth` — `"none"`, or a per-user secret **by code**: sugar for a `{secret.code}` header template.
  A scheme word like `"basic"`/`"bearer"` is refused: it reads as authenticated and attaches
  nothing. Fields outside this list are refused too — only `notes` and `description` are allowed
  next to the contract as free-form documentation. (Records that invented `secret_key` and `body`
  spent months sending unauthenticated requests and bare 401s; hence the strictness.)

Every template part also takes two server-side namespaces, substituted at call time and never seen
by the model as values:

- `{user.code}` — the calling user's stored param (timezone, account id, calendar path); an operator
  sets them in the console. Use these for anything deterministic per user — don't make the model
  carry a timezone through its context.
- `{secret.code}` — the calling user's secret. Headers by default; the secret's own `placements`
  setting opts it into `url` (for `?api_key=…` APIs) or `body`, and its `transform` setting handles
  static encodings (store `user:password`, set `base64`, write `"Authorization": "Basic
  {secret.code}"`). Values are scrubbed from responses. Details in
  [../reference/secrets.md](../reference/secrets.md).

Then write a skill that names the endpoint (as in the example above) so the agent knows when to use it.
This is not optional bookkeeping: endpoint records **do not appear in default `recall` results** (only
under `type=endpoint`), so an endpoint nobody's scenario mentions is effectively invisible. The
usual pattern is one endpoint record plus one scenario per task it serves — `server/src/octoforge_server/system_skills.py`
ships exactly that shape as an example (a weather endpoint and the scenario that reads it).

Users add their own secrets themselves: the agent mints a pre-filled one-time form link with the
`secret_link` tool (the user pastes only the value), or `/secrets` in Telegram returns an empty
form. Every secret needs a description — it is how the agent tells two secrets for one host apart.
See [../reference/secrets.md](../reference/secrets.md).

### Constraints worth knowing before you write one

- Outbound calls go through the SSRF guard: private, loopback and metadata addresses are refused. To call an
  internal service, either give it a publicly resolvable name or route through a gateway. The one exception is
  this installation's own API (`OF_SELF_BASE_URL`).
- Redirects are not followed.
- Response bodies are truncated at 8000 characters — return JSON, not HTML pages.
- Only `http`/`https`. Methods: the classic five plus the WebDAV family (`PROPFIND`, `PROPPATCH`,
  `REPORT`, `MKCOL`, `MKCALENDAR`, `COPY`, `MOVE`, `HEAD`, `OPTIONS`) — but not `LOCK`/`UNLOCK`.
- Parameters are strings. A body is a template with string placeholders; logic that must *compute* a
  body is where a [code tool](add-a-tool.md) starts making sense. Static value encodings (base64,
  hex digests) are the secret's `transform`, not template logic; format specs (`{x:>10}`) are
  rejected.

## Sharing and lifecycle

| Action | Who | How |
|---|---|---|
| Create | Any user | `instruction_save`, or the agent doing it on request |
| Publish to everyone | Operator | The console's publish action, or `admin_manage` in Telegram |
| Edit a published record | Its author | Ordinary save — publication moved visibility, not authorship |
| Shadow a public record for yourself | Any user | Saving over it creates your private copy |
| Delete | Its owner | `instruction_delete` (only your own) |
| Ship with the installation | Deployment | The system registry in code, plus `OF_SYSTEM_SKILLS_SOURCE` for per-installation tuning |

System records (`system=true`) are owned by the registry and cannot be edited by the agent. The overlay file
is the sanctioned way to change them per installation — replace content, append trigger phrases, or add
records — and a broken overlay is a logged warning, never a failed startup.

## Checking your work

1. Ask the agent to `recall` the phrasing a user would actually type, and see whether your record comes back
   at all.
2. Then ask for the task itself and watch which tools it calls.
3. If the record does not surface: shorten the title to the words people use, add trigger phrases in their
   language, or split the scenario. Ranking is cosine plus an exact-title boost, optionally reranked — a title
   that matches the query wins outright. Remember the diversity cap too: no type takes more than half the
   hits, so a scenario can be pushed out by memories or dataset descriptors rather than by other skills.
4. The operator console lists every record with its owner, version and usage count, which is the quickest way
   to see what is actually being retrieved.

## Code anchors

- `core/src/octoforge_core/instructions/registry.py` — the shipped scenarios, as writing examples
- `server/src/octoforge_server/system_skills.py` — an endpoint record plus its scenario
- `core/src/octoforge_core/net/tool_spec.py` — the endpoint document format and its validation
- `server/src/octoforge_server/skill_overlay.py` — the overlay format
- [../reference/instructions.md](../reference/instructions.md) — ranking, ownership, the sync
