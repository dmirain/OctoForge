# Dialog ownership

Which process runs a dialog's actor. One row per dialog records the owner, and everything that
matters when a second process exists — whether restart recovery may touch a dialog's stranded work,
and whether an actor may still answer for it — is decided from that row.

With a single process this is invisible: every claim succeeds, nothing is ever preempted, and
recovery matches everything it did before claims existed.

## How it works

`ConversationManager` claims a dialog when it builds the actor, keeps the claim warm with a
heartbeat, and stands the actor down when the heartbeat reports somebody else took over. The actor
knows only its own claim; the lifecycle lives in the manager.

A claim is two facts, kept apart because they answer different questions:

| Column | Question it answers |
|---|---|
| `generation` | Was I replaced? |
| `heartbeat_at` | Is anyone still alive on this dialog? |

### Claiming

`claim(dialog_id, owner)` always succeeds and bumps the generation. Placement is the router's
decision, not the database's: taking a dialog somebody else holds is a legitimate handover, not a
race to lose.

The identity of a claim is the **pair** `(owner, generation)`, never the number alone. Two processes
racing to take the same dialog may compute the same next generation, but only one owner survives in
the row, so the other fails its next check. This is also what retires an older actor in the *same*
process when a runner is rebuilt.

### Standing down

Every `CLAIM_HEARTBEAT_SECONDS` the manager refreshes the claims of its live runners in one
round trip. A dialog missing from the result was preempted, and its runner stands down:

- subscribers receive the end-of-stream marker, so transports close their streams and reconnect;
- the actor stops, leaving whatever was in flight for the new owner's recovery — the same shape a
  crash leaves behind;
- the manager deregisters it, so the next contact builds a fresh runner and claims again.

A run additionally checks ownership once when it starts (`_start_answer`), which closes the window
between a handover and the next heartbeat for *new* work. Nothing on the streaming path checks
anything: one query per run is nothing beside a model call, while a per-event check would put a
database round trip on the hot loop.

The check fails **open**. A database hiccup must not stop the single-process installation from
answering, and the heartbeat is what ultimately notices a lost dialog.

### Recovery happens on claim

Building a dialog's runner is what recovers that dialog: its `IN_PROGRESS`
exchanges are reopened, its orphaned tasks restarted, its unowned `OPEN`
exchanges resumed, its undelivered results re-sent. Starting up and taking a
dialog over are the same situation seen from two sides, so they run the same
code.

That matters most for the handover. A dialog moving mid-answer strands the
exchange its previous owner was working on, and no startup sweep will come
back for it — this process now holds a fresh claim, so every peer's recovery
skips the dialog. Recovering what you claim is the only thing that closes it.

### Recovery scope

`recover_interrupted()` only decides *which* dialogs this process may take;
the recovery itself is the claim path above. It touches dialogs no live
process owns.

Candidates come from the *work*, not from the claim table: dialogs holding an `IN_PROGRESS` or
`OPEN`-and-unowned exchange, plus the dialogs of orphaned and undelivered tasks. Rows stranded
before claims existed at all are therefore still recovered.

`held_elsewhere()` then removes whatever a **different** owner holds with a heartbeat newer than
`CLAIM_STALE_AFTER_SECONDS`. Two consequences:

- A process's **own** prior claim never blocks it. It just started, so it cannot be running that
  dialog, and waiting out the staleness window would stall every ordinary restart. This is why
  `OF_NODE_ID` must be stable across restarts of the same instance.
- A claim lookup that fails treats every candidate as somebody else's. Skipping recovery costs a
  delay; recovering a dialog another instance is actively running corrupts a live conversation.

A clean shutdown releases its claims, so its dialogs are free at once rather than after the window.

### Background work does not move a dialog

Claiming is unconditional because placement is a routing decision: a message arrived *here*, so this
process should own the dialog. Background work has no such decision behind it. The cron scheduler
and the collecting sweep both read tables that are global — every instance sees every due job and
every settled collection — so whoever polls first would take the dialog, and with several instances
polling every second a conversation would hop between them for no reason.

So background work follows the opposite rule from a message:

| The dialog is | What happens |
|---|---|
| already ours | acted on directly |
| held by nobody, or by a dead owner | adopted, exactly as a message would |
| held by a live peer | left alone — that peer sweeps too, and its own tick picks the work up |

A cron job whose dialog belongs elsewhere is **handed back at once** (`WakeOutcome.NOT_OURS`
releases the lease) rather than held for a lease TTL: both instances poll, so the owner fires it
within a tick or two. The collecting sweep simply skips it.

With one instance this changes nothing — `held_elsewhere()` never returns your own claims.

## Invariants

- **Claiming never fails.** The database records placement, it does not decide it.
- **A claim's identity is `(owner, generation)`.** Owner equality alone is not enough — the same
  process rebuilding a runner must retire the one it replaced.
- **Claiming a dialog recovers it.** Otherwise a handover mid-answer strands
  the exchange for good, because the fresh claim hides it from every peer's
  startup sweep.
- **Recovery never touches a dialog a live peer holds.** This is the rule that makes a second
  process safe; without it a starting instance resets exchanges its peers are answering.
- **Background work never takes a dialog.** A cron firing or a settled collection is acted on by
  the instance that already owns the dialog, or by anyone if nobody does. Only a message moves a
  conversation, because only a message was routed.
- **`reopen_in_progress` is scoped to one dialog.** There is no way to express the global reset that
  a single-process world could afford.
- **A stand-down is not a failure and not a user cancellation.** No error reaches the user; the work
  moved, and the new owner's recovery picks it up.
- **A stood-down runner never speaks again.** Subscribing to one hands back an already-closed queue
  rather than a silent one.
- **Single-process behavior is unchanged**, including immediate recovery after a restart.

## Configuration

| Variable | Effect |
|---|---|
| `OF_NODE_ID` | Names this process as a dialog owner. Defaults to the hostname. Must be stable across this instance's own restarts and unique against every other instance sharing the database |

`CLAIM_HEARTBEAT_SECONDS` (5 s) and `CLAIM_STALE_AFTER_SECONDS` (30 s) are constants in
`core/src/octoforge_core/agent/runner.py`, not settings: the staleness window carries several
heartbeats of slack on purpose, and tuning them independently is how a slow query gets mistaken for
a dead process.

## Failure modes

| Situation | Outcome |
|---|---|
| Another process claims a dialog mid-answer | The old actor stands down within one heartbeat; its streams close, clients reconnect, the exchange is left for the new owner's recovery |
| A message is submitted just after a handover | The run is refused before it starts; the exchange stays `OPEN`, which is work the new owner picks up |
| A dialog moves mid-answer | The new owner reopens the exchange and answers it. The old owner may still be streaming until its next heartbeat, so the text can visibly restart |
| A process dies without releasing | Its claims go stale after `CLAIM_STALE_AFTER_SECONDS`, then recovery may take them |
| Two instances share an `OF_NODE_ID` | They treat each other's live dialogs as abandoned, and recovery corrupts running conversations |
| An instance's `OF_NODE_ID` changes on restart | Correct but slower: its own stranded work waits out the staleness window instead of being recovered at once |
| The claim table is unreachable at startup | Nothing is recovered this start; the next restart, or the owning process, picks the work up |
| The claim table is unreachable during a run | The ownership check passes, the answer proceeds, and the heartbeat retries on its next tick |
| The claim lookup fails while placing background work | Treated as somebody else's: the firing is handed back and retried, rather than moving the dialog on a guess |

## Code anchors

- `core/src/octoforge_core/dialogs/api.py` — `DialogClaim`, `ClaimRepository`
- `core/src/octoforge_core/dialogs/store.py` — `SqlAlchemyClaimRepository`
- `core/src/octoforge_core/dialogs/models.py` — `DialogClaimRow`
- `core/src/octoforge_core/agent/runner.py` — `OwnershipConfig`, `ConversationManager._beat_once`,
  `ConversationManager.recover_interrupted`, `ConversationManager._runner_for_background`,
  `ConversationRunner.stand_down`, `STREAM_CLOSED`
- `core/src/octoforge_core/cron/api.py` — `WakeOutcome`
- `core/src/octoforge_core/db/migrations/versions/a4e9c2b7f513_dialog_claims.py` — the table
- `core/tests/test_dialog_claims.py` — the claim rules
- `core/tests/test_dialog_ownership.py` — recovery scope and stand-down
