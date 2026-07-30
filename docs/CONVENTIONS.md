# How this documentation is written

Rules for anyone — human or agent — editing files under `docs/`. They exist because the previous
documentation set failed in specific, repeatable ways, and was deleted rather than kept around to
confuse readers — git remembers it if anyone needs the archaeology.

## 1. The code is the truth

Every statement here describes what the code does now. Before writing that something works a
certain way, open the file and check. When documentation and code disagree, the documentation is
the bug.

Consequences:

- **Cite code.** Each reference page ends with a *Code anchors* section: the files and symbols the
  page describes. Point at them inline too when a claim is subtle.
- **No aspirations.** Planned work does not belong in a description of the system. If something is
  missing, it goes to [limitations.md](limitations.md) as a gap, not to a reference page as a
  promise.
- **`tools/check_docs.py` runs in `make check`.** It fails when a repository path mentioned in the
  docs no longer exists, or an internal link points nowhere. It cannot check semantics — that part
  is on you.

## 2. Present tense, no history

Write the current state. Not "the audit of 2026-07-26 moved ranking off the event loop" but
"ranking runs in a worker thread". Not "we replaced message injection with the pull model" but
"branches re-read the narrative at every iteration".

History belongs to git; the reasoning that is still load-bearing belongs in an *Invariants* or
*Why it is this way* section, phrased as a property of the system rather than as a story about the
change. Dates appear only where the fact itself is time-bound — a measurement, or a comparison
against another project's code at a point in time.

## 3. English

All of `docs/` is English, including comparisons, and so are code comments and commit messages. This
is not a preference about languages: the docs are the surface a stranger evaluates the project
through, and they must be readable by the same audience the README addresses. Conversation and
issue threads follow whatever language the participants use.

## 4. One aspect per file, one shape per page

`reference/` has a file per aspect of the system. Each follows the same skeleton, so a reader knows
where to look:

```markdown
# <Aspect>

One paragraph: what this is and which problem it owns.

## How it works
## Invariants        — properties that must hold; what breaks if they do not
## Configuration     — the OF_* variables that affect it (or "none")
## Failure modes     — what happens when it breaks, and what the operator sees
## Code anchors      — files and symbols, as a list
```

Sections that have nothing to say are dropped, not padded. `Invariants` is rarely one of them.

Concept, architecture, security and the guides are prose and do not follow the skeleton.

## 5. Say what is not there

A capability that is deliberately absent (shell tools, per-user web authentication) is documented
as a decision with its reason. A capability that is absent because nobody wrote it yet is
documented as a gap. Both live in [limitations.md](limitations.md), and reference pages link to it
rather than staying silent. Silence reads as "supported" to someone deciding whether to trust the
project.

## 6. Comparisons are dated and sourced

`comparisons/` states, per file, which version of the other project was read and whether the claims
come from its source code or its documentation. Comparative claims about *our* side are verified
against current code, not against an older comparison. See
[comparisons/README.md](comparisons/README.md).

## 7. Keep the map current

`docs/README.md` lists every page. A new page that is not in the map does not exist for readers.

## 8. Update docs in the same change as the code

A behavior change lands with its documentation edit in the same commit. The pull-request checklist
asks for it. A reference page that describes yesterday's behavior is worse than no page, because it
is trusted.
