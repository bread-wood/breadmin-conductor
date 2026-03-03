Write the Low-Level Design (LLD) document for a single module.

## When to Run

Dispatched by design-worker Phase 2 in parallel with other LLD agents,
after the HLD has been merged.

## Inputs

From the session prompt:
- `Repository` — `owner/repo` of the target GitHub repo
- `Milestone` — the version being designed (e.g. `calculator-v0.1.x`)
- `Module` — the module name (e.g. `runner`, `cli`, `config`)
- `Issue` — the `Design: LLD for <module>` GitHub issue number
- `Branch` — the branch to commit to

## Execution

### Step 0 — Check Out Branch

```bash
DEFAULT_BRANCH=$(gh repo view --repo <owner>/<repo> --json defaultBranchRef --jq '.defaultBranchRef.name')
git fetch origin
git checkout <branch>
git rebase origin/$DEFAULT_BRANCH
```

### Step 1 — Read the HLD

```bash
gh api repos/<owner>/<repo>/contents/docs/design/<milestone>/HLD.md --jq '.content' | base64 -d
```

Read the full HLD. Pay attention to:
- The `### Module: <name>` section for your specific module
- Interfaces this module exposes to other modules
- Dependencies on other modules

### Step 2 — Read Relevant Research Docs

```bash
# List all research docs for this milestone
gh api repos/<owner>/<repo>/contents/docs/research/<milestone> --jq '.[].name'
```

Read the research docs relevant to this module. The HLD's module section should
indicate which research findings apply.

### Step 3 — Write the LLD

Create `docs/design/<milestone>/lld/<module>.md` in the local checkout.

The LLD must cover:

**Overview**
- Module responsibility (one paragraph)
- What this module does NOT do (scope boundary)

**Public interface**
- All public functions / classes / methods exposed to other modules
- Type signatures with brief descriptions
- Error types raised

**Data structures**
- Key types, dataclasses, or schemas defined in this module
- Field descriptions and invariants

**Key algorithms and logic**
- Non-obvious algorithmic choices with rationale
- State machine or flow diagram if applicable (ASCII)
- Edge cases and how they are handled

**Internal structure**
- Private helpers and their purpose
- File layout within this module's scope

**Error handling**
- What errors this module can raise and under what conditions
- What errors from dependencies it catches and re-wraps vs. propagates

**Testing strategy**
- What to unit-test (pure functions, data transformations)
- What to integration-test (I/O, subprocess calls, external APIs)
- What to mock and why
- Concrete test case examples for the trickiest logic

**Dependencies**
- Other modules this module imports from
- External libraries used and why

### Step 4 — Commit the LLD

```bash
mkdir -p docs/design/lld
git add docs/design/<milestone>/lld/<module>.md
git commit -m "docs: add LLD for <module> (Closes #<issue_number>)"
git push
```

### Step 5 — Create PR

```bash
gh pr create \
  --repo <owner>/<repo> \
  --title "Design: LLD for <module>" \
  --body "Closes #<issue_number>" \
  --base $DEFAULT_BRANCH
```

### Step 6 — STOP

Do not merge. The orchestrator monitors the PR and merges when CI passes.

## Constraints

- **One PR, one doc**: this agent produces only `docs/design/<milestone>/lld/<module>.md`
- **Respect module scope**: only describe the module named in the session prompt
- **No implementation code**: this agent writes documentation only
- **Concrete recommendations**: every design decision must have a rationale,
  not just a list of options
