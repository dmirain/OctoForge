# Access: statuses, the registration queue and operator settings

Who may talk to the installation at all. Every person carries one of three statuses —
`waiting`, `active`, `banned` — and every surface asks the same question at its door. The number of
active users is capped by an operator setting stored in the database, so a public installation can
open registration exactly as wide as its hardware and budget allow, and change that width from the
console without a redeploy.

## How it works

**Everyone is born waiting.** A newcomer's first contact mints their person (`identity`) with
status `waiting`, and immediately attempts activation: `AccessService.admit` reads the cap and runs
one conditional UPDATE — become `active` if the count of active users is under the cap. With no cap
set, everyone activates on first contact, which is why an installation that never opens the
settings tab behaves exactly as before.

**The queue drains as its people knock.** `admit` runs on *every* message of a waiting person, so
when a slot frees — a ban, a raised cap — the next message of whoever knocks first takes it. There
is no background sweep and no automatic promotion: freed slots are taken at the door or handed out
by the operator's activate button. A queued person is told they are waiting and **never told their
position** — a number the installation cannot promise is worse than none.

**The operator outranks the cap.** Activation from the console applies even past a full house;
`banned` is the operator's door — the person's data stays, their access does not. A ban pauses
every cron job they have (a banned user must not keep spending through the scheduler); an unban
deliberately resumes nothing. Activating somebody who was waiting sends them a Telegram notice when
the surface can reach them.

**Where the gate runs.** Telegram checks after the invite gate and before any dispatch (with the
invite gate closed, a stranger without a code still gets nothing, not even a queue notice); the
notice repeats at most once per person per day. The HTTP API checks inside `get_user_id`, so a
waiting or banned person gets 403 from every dialog endpoint. Both gates resolve the core person
first — statuses, like tariffs, are filed under the person, never under a surface handle.

**In the split arrangement the service is the gate.** A standalone Telegram ingestion node has no
core database and cannot resolve a person, so it does not run this gate at all — it posts the
message and the service refuses. That 403 carries an `X-Access-Status` header naming the status,
which the node turns into the same queue or closed-door notice, with the same once-a-day dedup. The
header exists for exactly this: without it the refusal would be an anonymous transport error, and a
queued newcomer would sit in silence wondering whether the bot was broken.

**Referrals do not bypass the cap.** A member's `/invite` link opens the *invite* gate for a
friend and records who brought whom ([telegram.md](telegram.md)); the friend then queues through
this cap like anyone else. The cap has exactly two doors: a free slot at the knock, or the
operator's hand.

**Opening a public installation.** The invite gate and this cap are alternative doormen, not
layers to stack. A public bot sets `OF_TELEGRAM_OPEN_REGISTRATION=true` — anyone may start talking
— and regulates *how many* get in with `max_active_users` from the console. Keeping both is
possible but pointless: the invite code would already have decided who gets in.

## Operator settings

`app_settings` is a generic key→value table (`settings/` module) edited from the console:
`GET/POST /api/admin/settings`, `DELETE /api/admin/settings/{key}`. Settings live in data rather
than environment because they govern behavior *while the process runs* — changing one must not need
a redeploy. Keys follow the params/secrets grammar (`[a-z0-9_]{1,64}`); an absent key is a meaning
("never set"), not an error.

| Key | Meaning |
|---|---|
| `max_active_users` | How many people may be `active` at once. Absent = no cap; `0` = no free slots — the operator's pause button on registration. A malformed value fails open (no cap), never closed. |

## Invariants

- **Zero configuration = zero behavior change.** No cap set: everyone activates at first contact.
- **The cap is never exceeded by admission.** The head-count lives in the UPDATE's own WHERE, and
  on Postgres the activation additionally takes a transaction advisory lock — unlike the spend
  guardrails, this cap is a promise, and two nodes admitting at once cannot both slip past it.
  Only the operator's hand goes past the cap, on purpose.
- **Nobody is demoted by automation.** `try_activate` touches only the WAITING; lowering the cap
  strands no one — it only stops new admissions.
- **The queue position is never shown**, on any surface, in any message.
- **A ban keeps the data.** Statuses flip; rows never disappear.

## Failure modes

| Situation | Outcome |
|---|---|
| Two nodes admit at the boundary | The advisory lock serializes them; exactly one wins the last slot |
| `max_active_users` holds garbage | Fails open (no cap) and registration keeps working; fix the value in the console |
| Activation notice cannot be delivered | The activation stands; `notified: false` in the response |
| Banned user keeps writing | One notice per day on Telegram, 403 on the API, nothing dispatched |

## Code anchors

- `core/src/octoforge_core/identity/api.py` — `UserStatus`, `set_status`, `try_activate`
- `core/src/octoforge_core/identity/service.py` — `AccessService.admit`
- `core/src/octoforge_core/settings/api.py` — the settings port and `max_active_users`
- `surfaces/telegram/src/octoforge_telegram/poller.py` — the surface gate (`_admit`)
- `server/src/octoforge_server/deps.py` — the API gate (`get_user_id`)
- `core/tests/test_identity.py`, `core/tests/test_settings_store.py`,
  `deploy/tests/test_telegram_poller.py`, `deploy/tests/test_admin_api.py`
