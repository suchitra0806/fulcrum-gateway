# Issue Workflow: Creation, Labeling, and Consumption

This document defines how we work out of GitHub issues in this repository: how
to file an issue, exactly which labels to apply (and who applies them), and how
to pick your next issue. The pipeline labels defined here are strictly about
prioritization of work — they are orthogonal to descriptive labels like
`documentation` or `question`, which coexist with them and are sorted out in
[section 5](#5-orthogonal-labels). PR mechanics live in
[CONTRIBUTING.md](./CONTRIBUTING.md); this document covers the issue lifecycle.

The short version: **GitHub issues are the board.** We run light-touch Scrumban
on one-week sprints. Labels are the swimlanes, the pull order is top-down, and
self-assignment is how we avoid duplicate effort.

---

## 1. Creating issues

**If you're doing the work, it lives in GitHub.** No off-board work. Write the
issue even when you intend to fix it yourself in the next ten minutes — writing
it down forces an honest priority call, and whether to fix it now or let it
queue is then a separate, explicit decision.

### Bugs

1. File the issue: what's broken, how to reproduce, observed vs. expected.
2. Apply the **`bug`** label.
3. Apply exactly one priority label, **`P1`–`P5`**, using the rubric in
   [section 3](#3-pipeline-labels-the-swimlanes). This is an engineering call —
   you do not need permission to set a bug's priority.
4. Then decide: fix it now, or let it queue. Either is fine. Writing the issue
   is the bar; the timing is your call.

### Ideas and feature proposals

1. File the proposal as an issue: clear title, context, and *why* — what signal
   or customer need motivates it.
2. Apply the **`enhancement`** label (or **`documentation`** /
   **`question`** if that's a better fit).
3. **Do not** apply `roadmap` or `P0` yourself — those are leadership calls
   (see below). Your proposal will be picked up on the next grooming pass.

> **Sensitive or speculative ideas:** this is a public repository. If a
> proposal involves unannounced partners, pitches, or anything not ready for
> public eyes, file it in a private repo instead and link it from there during
> grooming.

### Who applies which labels

| Label | Applied by | When |
| --- | --- | --- |
| `bug` + one of `P1`–`P5` | The engineer filing the bug | At creation |
| `enhancement`, `documentation`, `question` | The filer | At creation |
| `roadmap`, `epic: <name>` | Leadership (Sean + Mark) | Weekly grooming pass |
| `P0` | Leadership (Sean + Mark) | When a commitment depends on it |
| `re-evaluating` | Leadership (Sean + Mark) | When sprint fit of a roadmap-lane item is being reconsidered |
| Resolution labels (`duplicate`, `invalid`, `wontfix`, `deferred`) | Whoever closes/parks the issue | At close or park |
| Automation labels (see [section 5](#5-orthogonal-labels)) | Bots | Never by hand |

---

## 2. The pipeline: three swimlanes, one board

Every *triaged* issue sits in **exactly one** base lane — Stability via one of
`P1`–`P5`, or Roadmap via `roadmap` and/or an `epic: <name>` label — and may
additionally carry `P0` stacked on top. Top-to-bottom is the order we pull
from:

| Lane | Label(s) | What it is |
| --- | --- | --- |
| **Commitment-Blocking** | `P0` | Drop-everything work: demo-flow breakers, upcoming-pitch needs, fixes that signal we're serious to a partner. Later: signed contracts with delivery dates. |
| **Stability** | `P1`–`P5` | Bugs, prioritized by the severity rubric below. Keeps the product healthy and trust intact. |
| **Roadmap** | `roadmap`, `epic: <name>` | Features and investments Sean and Mark queue up each week in grooming. The default thing we work on. An `epic: <name>` label counts as roadmap-lane membership on its own — see below. |

Two clarifications, made explicit here because labels have to be mechanically
unambiguous:

- **`P0` is not the top of the severity scale.** `P1`–`P5` measure *severity*
  and are an engineering call. `P0` measures *commitment* — applied only by
  leadership, and it can land on a bug or a feature alike. `P0` **stacks on
  top** of an existing `P1`–`P5` or `roadmap` label rather than replacing it:
  the underlying label keeps recording what the issue is and how severe, while
  `P0` says a commitment now depends on it. When `P0` is present, it wins —
  the issue sits in the Commitment-Blocking lane regardless of what's under
  it, and de-escalation is just removing `P0` (the issue falls back to its
  prior lane). A `P0` does **not** require an underlying label; leadership can
  apply it bare.
- **Not every open issue has a pipeline label, and that's by design.** Bugs get
  a priority at filing, but a feature proposal legitimately has *no* pipeline
  label until the grooming pass promotes it to `roadmap` (or declines it). An
  issue with a type label but no pipeline label means **awaiting grooming** —
  it is not in any lane yet and is not fair game to pull. A *bug* with no
  priority label is mislabeled; fix it.

---

## 3. Pipeline labels: the swimlanes

### `P0` — Commitment-Blocking

- **What qualifies:** breaks the demo flow; needed for an upcoming pitch; a fix
  that signals we're serious about a partner; eventually, contractual delivery
  dates.
- **Who applies it:** leadership only.
- **How it combines:** `P0` stacks on top of whatever the issue already has —
  `bug` + `P2` + `P0` and `roadmap` + `P0` are both well-formed, and a bare
  `P0` with no other lane label is fine too. While `P0` is on, the issue is in
  the Commitment-Blocking lane, full stop.
- **How to handle it:** drop everything if asked directly. Otherwise, you can't
  go wrong picking one up when you see it — just self-assign. Startup reality:
  `P0` sometimes means optics, not technical severity. That's fine; the label
  means "a commitment depends on this," not "the code is on fire."

### `P1`–`P5` — Stability severity rubric

Exactly one per bug, applied by the filing engineer. The same scale on every
bug ticket so triage isn't a debate:

| Label | Severity | Definition |
| --- | --- | --- |
| `P1` | Critical | Critical security vulnerability, authorization issue, or breaks multiple major workflows. |
| `P2` | High | Major feature broken or widespread degradation; would-be P1s that probably have workarounds; standard "high priority" issues with no workaround. |
| `P3` | Medium | Workflow blocker with a lower blast radius, or a blocker with a clear workaround. |
| `P4` | Low | Cosmetic or generally low-impact issues. Also: blockers on a workflow you're not sure anyone cares about. |
| `P5` | Trivial | Nice-to-have polish; fixed when capacity allows. |

Rule of thumb: **~20% of sprint capacity is reserved for stability work.** If
the P1/P2 count climbs, that share goes up.

### `roadmap` — Planned work

- **What lives here:** features and investments tied to the sprint direction,
  queued by Sean and Mark in the weekly grooming pass.
- **Who applies it:** leadership only. Filing an `enhancement` issue is how you
  *propose* roadmap work; the label is how it gets *accepted*.
- **Pulling from it:** anything labeled `roadmap` or `epic: <name>` is fair
  game — self-assign and go.

#### `epic: <name>` — grouping roadmap work

When a body of roadmap work is big enough to need grouping, leadership creates
an epic label — `epic: <short-name>`, e.g. `epic: hermes-demo` — and applies
it to the member issues during grooming.

- An epic label is used **with `roadmap` or instead of it**: `epic: <name>`
  by itself already places the issue in the roadmap lane. Adding `roadmap`
  alongside is fine but not required.
- One epic per issue. If an issue seems to belong to two epics, it's probably
  two issues.
- `P0` stacks on epic'd issues the same as anywhere else.
- When pulling roadmap work, prefer finishing issues in an epic that's already
  in flight over opening a new front.
- Epic labels are created and applied by leadership, like `roadmap` itself.
  When the epic ships, the label stays on the closed issues as the record of
  what it covered.

#### `re-evaluating` — roadmap membership under review

When the team is reconsidering whether a roadmap-lane item still belongs in
the sprint, leadership stacks `re-evaluating` on it. The issue is **not
deferred** — it keeps its `roadmap` / `epic: <name>` label(s) and stays in the
lane — but it is **not a good pull** while the label is on: don't self-assign
it. Grooming resolves it one of two ways: remove `re-evaluating` (the issue is
fair game again) or park it with `deferred` (which, as usual, removes its lane
labels).

- **Who applies it:** leadership only, like `roadmap` and epic labels.
- **What it is not:** a severity or resolution call. It says nothing about how
  important the work is — only that sprint membership is an open question.

---

## 4. Consuming issues: the pull order

Finished something? Walk the lanes top-down, then self-assign:

1. **Any `P0` unassigned?** Grab it. You can't go wrong picking one up when you
   spot it.
2. **Haven't hit your roadmap minimum yet?** Pull a roadmap-lane issue
   (`roadmap` or `epic: <name>`) that fits the sprint direction — skipping
   anything marked `re-evaluating`. Roadmap is the default thing we work on,
   and the minimum is a floor — not the target.
3. **Past the minimum, read the week.** It's a judgment call between another
   roadmap item and a bug, given where we are: if the roadmap lane is heavy or
   the sprint direction needs the push, take more roadmap; if P1/P2s are
   piling up or stability needs the attention, work bugs by severity. Neither
   is the automatic answer.

Expectations that make this work:

- **Self-assign the moment you start.** That's the whole mechanism for avoiding
  duplicate effort. An unassigned issue is available; an assigned one is taken.
- **One self-assigned issue at a time.** Self-assignment means "actively
  working on this," so in general you should hold only one. The exceptions are
  real: you're blocked on an issue, or you had to pause it for something
  higher priority (a `P0` pull is the typical case). If you're holding more
  than one for any other reason, unassign what you're not actually working —
  it's hiding available work from the rest of the team.
- **At least one `roadmap` issue per person, per one-week sprint.** Beyond
  that, use judgment on the roadmap-vs-bug mix — read the week's signal. If the
  roadmap lane looks light, lean harder into stability; if it's heavy,
  prioritize it.
- **Reviews are free wins.** You can't go wrong reviewing an open PR before
  pulling new work.
- **When unsure, ping Sean or Mark.**

---

## 5. Orthogonal labels

The pipeline labels say *when* work gets pulled. Everything else on the label
list is orthogonal — it describes what an issue *is*, who it's *for*, or how it
was *resolved*, and combines freely with (or exists independently of) the
pipeline.

### Type labels — set by the filer, pair with pipeline labels

| Label | Use |
| --- | --- |
| `bug` | Something isn't working. Always pairs with one of `P1`–`P5`. |
| `enhancement` | New feature or request. Pairs with `roadmap` once groomed. |
| `documentation` | Docs improvements or additions. |
| `question` | Further information is requested; may never enter a lane. |

### Routing labels — who should pick this up

| Label | Use |
| --- | --- |
| `good first issue` | Small, well-scoped, good for newcomers. Any maintainer can add it. |
| `help wanted` | Extra attention or outside contribution welcome. |
| `ready-for-agent` | Triage complete; the issue is specified tightly enough for an AFK agent to pick up unsupervised. Orthogonal to lane — a `P3` bug or a `roadmap` feature can both be `ready-for-agent`. |
| `ux` | The heart of the issue is user experience — CLI ergonomics, error messages, output readability, workflow friction. Routes it toward someone who'll sweat the experience, and signals the fix should be judged on how it feels, not just whether it works. Pairs with any lane. |

### Resolution and status labels — applied at close or park

| Label | Use |
| --- | --- |
| `duplicate` | Already tracked elsewhere; close with a link to the original. |
| `invalid` | Doesn't seem right / not actionable as filed. |
| `wontfix` | Deliberate decision not to do this; say why when closing. |
| `deferred` | Parked: real, but consciously not now. A deferred issue keeps its type label but leaves its lane (remove the pipeline label). Re-entry happens via grooming. |
| `re-evaluating` | Roadmap item under review: not deferred, but not a pull candidate while the team decides whether it still belongs in the sprint. Keeps its lane label(s); resolved in grooming. See [section 3](#3-pipeline-labels-the-swimlanes). |

### Automation labels — hands off

These are applied by bots (Release Please, Dependabot, the PR labeler). Never
apply or remove them manually; they're not part of the workflow above.

| Label | Owner |
| --- | --- |
| `autorelease: pending` | Release Please |
| `dependencies` | Dependabot |
| `python`, `javascript`, `github_actions` | PR labeler (by file type) |

---

## 6. Label inventory for setup

The pipeline labels and `ux` are new and need to be created once (the other
orthogonal labels above already exist). For whoever runs the setup:

```bash
gh label create "P0"      --color b60205 --description "Commitment-blocking: drop everything (leadership-applied)"
gh label create "P1"      --color d93f0b --description "Critical: security/authz issue or breaks multiple major workflows"
gh label create "P2"      --color e36209 --description "High: major feature broken or widespread degradation"
gh label create "P3"      --color fbca04 --description "Medium: workflow blocker with lower blast radius or clear workaround"
gh label create "P4"      --color d4c5f9 --description "Low: cosmetic or low-impact"
gh label create "P5"      --color c2e0c6 --description "Trivial: polish, fixed when capacity allows"
gh label create "roadmap" --color 2da44e --description "Planned work queued in grooming (leadership-applied)"
gh label create "ux"      --color bfd4f2 --description "User experience is the heart of the issue: ergonomics, errors, output, friction"
gh label create "re-evaluating" --color d4a72c --description "Roadmap item under review: not deferred, but not a pull candidate while sprint fit is reconsidered"
```

Epic labels are created on demand by leadership as epics are opened, all with
the same color so they read as one family:

```bash
gh label create "epic: <name>" --color 5319e7 --description "<one-line epic summary>"
```

---

## Quick reference

- Filing a bug? → `bug` + one of `P1`–`P5`. Your call on both.
- Filing an idea? → `enhancement` only. Leadership adds `roadmap`/`P0` in
  grooming.
- Picking work? → `P0` first; `roadmap` until you've hit your minimum; past
  that, judgment between more roadmap and bugs — read the week. Self-assign
  immediately.
- One self-assigned issue at a time, unless blocked or pulled onto something
  higher priority.
- One base lane per issue (`P1`–`P5`, or roadmap via `roadmap` and/or
  `epic: <name>`); `P0` may stack on top. No pipeline label at all = awaiting
  grooming.
- `re-evaluating` on a roadmap item = leave it alone for now. It's neither
  deferred nor pullable; grooming will settle it.
- `P0` ≠ severity. It means a commitment depends on it, and it outranks
  whatever label sits under it.
- Never touch bot labels.
