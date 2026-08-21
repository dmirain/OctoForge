# Adding a tool

When a capability needs code — a protocol the agent cannot express as an HTTP contract, a computation, a
system only reachable through a library. If it *can* be expressed as an HTTP contract, store an endpoint
record instead and skip this entirely: [author-skills-and-endpoints.md](author-skills-and-endpoints.md).

## The shape

A tool is a class with `spec` and `execute`:

```python
from typing import Any

from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

NAME = "invoice_status"
DESCRIPTION = (
    "Check the status of one invoice by its number. Use this instead of guessing "
    "from earlier messages; the answer is authoritative."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "number": {"type": "string", "description": "Invoice number, e.g. INV-2031"}
    },
    "required": ["number"],
}


class InvoiceStatusTool:
    """Reads invoice status from the billing service."""

    def __init__(self, billing: BillingClient) -> None:
        self._billing = billing

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=NAME, description=DESCRIPTION, parameters_schema=SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        number = arguments.get("number")
        if not isinstance(number, str) or not number.strip():
            raise ToolArgumentsError("number must be a non-empty string")
        status = await self._billing.status(number, user_id=context.user_id)
        return f"{number}: {status.state}, due {status.due_on}"
```

Then register it in your composition root:

```python
registry.register(InvoiceStatusTool(billing_client))
```

## Rules that are not optional

**Identity comes from the context.** `context.user_id` decides whose data you touch. Never accept an owner id
as an argument — the model can be talked into passing someone else's.

**Return text, not structure.** The model reads your output. Format it for reading: short, labelled, no raw
JSON dumps unless the shape is the point.

**Validate and explain.** Raise `ToolArgumentsError` with a sentence that tells the model how to fix the call.
Consider returning the contract itself on a validation failure — that is what `external_call` does, and it
turns two round trips into one.

**Fail as data.** Do not let infrastructure exceptions escape as control flow: the loop already converts them
into `error: …` output, but a message like `KeyError: 'items'` teaches the model nothing. Catch what you
expect and return a sentence.

**Bound your output.** A tool that can return a megabyte will flood the context and cost real money. Truncate
with a marker, or return a summary plus a way to fetch more.

**Cap the cost.** Give your client a timeout. The run's cancellation aborts your coroutine, but nothing else
limits how long you block a process slot.

## Description writing

Tool descriptions are the only guidance always in the prompt (stored skills arrive by search), so they carry
policy, not just mechanics. Say what the tool is for, when to prefer it over another, and what not to use it
for. Compare `recall` ("first call for any non-trivial request"), `http_request` ("search for a stored
endpoint first") and `task_create` ("make the prompt self-contained"). Keep the wording consistent with the
system prompt's rules; they are maintained together on purpose.

## Conditional visibility

If a tool should exist only for some callers, give it a `visible_to` hook:

```python
    def visible_to(self, context: ToolContext) -> bool:
        return context.user_id in self._allowed
```

The registry consults it per invocation, so one registry serves different tool lists per dialog — that is how
`admin_manage` hides from non-admins and `image_look` hides when vision is unavailable.

If a tool should exist only when configured, do not register it at all:

```python
if settings.billing_api_key:
    registry.register(InvoiceStatusTool(billing_client))
```

Both keep the prompt free of tools that would only fail — and keep the tool list stable within a run, which
provider prompt caching depends on.

## Where the code goes

In this repository: implementations live in the owning domain module's `tools.py`
(`instructions/tools.py`, `cron/tools.py`, `net/tools.py`, …), never in `tools/` — that package is framework
only, and `core/tests/test_boundaries.py` enforces it. In your own application, anywhere; you only import the
framework.

## Tests

A tool test needs no LLM: construct the tool with a fake client, call `execute` with the arguments a model
would send, assert on the returned text and on what the fake received. Add the failure cases — bad arguments,
backend error, oversized response — because those are the paths the model will actually hit.

## Code anchors

- `core/src/octoforge_core/tools/base.py` — `Tool`, `ToolSpec`, `ToolContext`
- `core/src/octoforge_core/tools/registry.py` — registration and visibility
- `core/src/octoforge_core/net/tools.py` — a thorough example (validation, guard, truncation)
- `core/src/octoforge_core/instructions/tools.py` — an example whose description carries policy
- [../reference/tools-framework.md](../reference/tools-framework.md) — the full contract
