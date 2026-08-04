# The tools framework

What a tool is, how it is registered, how it sees the dialog it runs in, and why a failing tool does not
break a run. The framework is deliberately small: a protocol, a registry, three errors.

## How it works

A tool is anything with a `spec` and an `execute`:

```python
class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...
    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str: ...
```

`ToolSpec` is the LLM-facing description — `name`, `description`, `parameters_schema` (JSON Schema).
`execute` receives whatever the model produced as arguments and returns text.

`ToolRegistry` holds tools under unique names (`DuplicateToolError` on a clash, `ToolNotFoundError` on
lookup) and answers `specs(context)` with the tools visible for that invocation.

The framework package imports no domain module. Tool *implementations* live in their own domain module's
`tools.py` (`instructions/tools.py`, `cron/tools.py`, `net/tools.py`, …) and are registered in the
composition root. This is enforced by `core/tests/test_boundaries.py`.

### ToolContext: what a tool knows about its dialog

| Field | Meaning |
|---|---|
| `user_id`, `channel`, `dialog_id` | Who and where. Ownership of every stored record comes from here, never from tool arguments |
| `task_spawner`, `task_deleter` | Bound to the actor; absent outside a dialog, and the task tools report that instead of failing to construct |
| `user_prompter` | Backs `ask_user`; parks the run's exchange |
| `image_inspector` | Backs `image_look`; `None` when vision or the image resolver is unavailable |
| `owner_task_id` | The task this invocation belongs to, so a tool can recognize acting on itself |

Because identity comes from the context, a tool cannot be talked into operating on another user's data
by putting a different id in its arguments.

### Conditional visibility

A tool may define `visible_to(context) -> bool`. The registry consults it in `specs(context)`, so the
same registry serves different tool lists per dialog:

- `image_look` hides itself when no `image_inspector` is bound;
- `admin_manage` hides itself from non-admins.

Tools whose *existence* depends on configuration rather than on the caller are simply not registered —
`web_search` without a token, `secret_list`/`secret_link` without `OF_SECRETS_KEY`, for example. Both
mechanisms keep the prompt free of tools that would only fail.

### Errors are data

`AgentLoop` catches everything a tool raises and hands the model `error: <message>` as the tool's output
(class name included, because some exceptions stringify to nothing). The run continues, and the model
usually corrects itself.

`ToolArgumentsError` is the conventional way to reject bad arguments. Several tools go further and
return the *contract* in the error — `external_call` includes the endpoint's declared parameter schema,
so a blind call self-corrects in one step instead of guessing.

### Descriptions carry policy, not just mechanics

Tool descriptions are the only guidance that is always in the prompt (stored skills arrive by search),
so they state policy: `recall` presents itself as the first call for any non-trivial request,
`http_request` and `web_search` demote themselves in favour of stored endpoints and stored knowledge,
`task_create` insists that a prompt be self-contained. The system prompt's first rules say the same
thing; the two are kept in sync deliberately.

## Invariants

- **Tool names are unique per registry**, and registration is explicit — there is no discovery by scan.
- **Identity comes from `ToolContext`.** A tool never trusts an id in its arguments.
- **A tool returns text.** Structured results are formatted by the tool for the model to read.
- **A raising tool never ends a run.** Only provider-level failures do.
- **Tool visibility is resolved once per run**, keeping the tool list stable across iterations for
  provider prompt caching.
- **The framework imports no domain module**, and domain modules do not import each other's internals.
- **A tool cannot delete the task it runs in.**

## Configuration

The framework itself has no settings. Which tools exist depends on configuration — see
[configuration.md](configuration.md); the current set is listed in the repository
[README](../../README.md).

## Failure modes

| Situation | Outcome |
|---|---|
| Model calls a tool that does not exist | `ToolNotFoundError` → `error: …` output; the model sees the mistake |
| Model passes invalid arguments | `ToolArgumentsError` → error output, often with the declared schema |
| Tool raises an unexpected exception | Formatted and returned as error output; run continues |
| Tool hangs | Bounded by the run's own cancellation and by whatever timeout the tool's client uses |
| Two tools registered under one name | `DuplicateToolError` at startup — a wiring bug, caught immediately |

## Code anchors

- `core/src/octoforge_core/tools/base.py` — `Tool`, `ToolSpec`, `ToolContext`, and the actor-bound ports
  (`TaskSpawner`, `TaskDeleter`, `UserPrompter`, `ImageInspector`)
- `core/src/octoforge_core/tools/registry.py` — the registry and visibility
- `core/src/octoforge_core/tools/errors.py` — the three errors
- `core/src/octoforge_core/composition.py` — where every tool is wired
- `core/tests/test_tools_registry.py`, `core/tests/test_boundaries.py`
