# Comparisons

Code-level studies of three adjacent projects, and what OctoForge takes, refuses or does differently.

## Method and scope

Each study was made by reading the other project's **source code**, not its marketing or its documentation —
one file per project, listing the mechanisms that matter and how the same problem is solved here. Maturity,
popularity, team size and license are out of scope: this is about mechanics and architecture.

| Study | Project | What it is | Sources read |
|---|---|---|---|
| [openclaw.md](openclaw.md) | [openclaw](https://github.com/openclaw/openclaw) | Personal AI assistant, TypeScript, one gateway process across ~20 messaging channels | Source, July 2026 |
| [opencode.md](opencode.md) | [opencode](https://opencode.ai) | Open-source AI coding agent, TypeScript monorepo, CLI/TUI | Source (branch `dev`), July 2026 |
| [hermes-agent.md](hermes-agent.md) | [hermes-agent](https://github.com/NousResearch/hermes-agent) | Self-improving single-user CLI agent, Python | Source, July 2026 |

Two rules keep these honest:

- **Their side is dated.** The observations describe the code as read in July 2026. These projects move; treat
  every claim about them as a snapshot, and re-read before relying on it.
- **Our side is current.** Every claim about OctoForge is verified against the code in this repository at the
  time of writing, not copied from an older study. Where an earlier gap has since been closed, the study says
  what the current behavior is.

## Why compare at all

Three independent projects solving overlapping problems is the cheapest available review of our own design.
A mechanism that all three implement is a strong signal we are missing something; a mechanism all three
avoid is worth understanding before adopting. The studies are therefore written to be useful in both
directions — what to take, and what to deliberately not take.

## What the three have in common that OctoForge does not

- **Single-user by construction.** All three assume one owner. Multi-tenancy, where they have it, is
  "one container per customer". OctoForge is multi-user in the schema.
- **Host access.** All three can run commands or edit files, with approval systems, sandboxes and policies
  around that capability. OctoForge has no shell and no filesystem tools, so none of that machinery exists
  here — see [../limitations.md](../limitations.md).
- **Local-first operation.** They run on the user's machine and treat the filesystem as state. OctoForge is a
  server with a relational database.

## What OctoForge has that none of the three does

- **Obligations as durable rows** (exchanges) with concurrent answers per conversation, instead of one active
  run per session plus a queue.
- **Capability as owned data**: skills, knowledge and callable endpoint contracts as rows found by embedding
  search, with per-user ownership, publication and a declarative system slice.
- **Per-user secrets structurally outside the prompt path**, host-bound and scrubbed.
- **A multi-user schema and a read-only cross-user operator view** as the only exception to owner scoping.
