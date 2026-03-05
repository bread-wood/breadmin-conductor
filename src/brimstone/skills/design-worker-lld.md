Write the Low-Level Design (LLD) document for a single module.

**Do NOT use the Agent tool. Do NOT spawn sub-agents. Do NOT explore the repo broadly.**
Read only the specific files listed in the steps below.

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
- `Working Directory` — absolute path to the isolated worktree (already checked out)

## Execution

### Step 0 — Enter Your Working Directory

```bash
cd <Working Directory from Session Parameters>
```

The branch is already checked out. Do NOT run `git checkout` or `git rebase`.

### Step 1 — Read the HLD

```bash
cat docs/design/<milestone>/HLD.md
```

Read the full HLD. Focus on the `### Module: <module>` section for your specific module:
- Interfaces this module exposes to other modules
- Dependencies on other modules

### Step 2 — Read Relevant Inputs

Read only the files below. Do NOT explore other directories.

```bash
# List research docs for this milestone
ls docs/research/<milestone>/

# Read each research doc relevant to this module
# (the HLD's module section will indicate which ones apply)
cat docs/research/<milestone>/<relevant-doc>.md

# Read the existing source file for this module (if it exists)
cat <module>/*.py 2>/dev/null || true

# Read the test file for this module (if it exists)
cat tests/test_<module>.py 2>/dev/null || true

# Read the previous-milestone LLD for this module (if it exists)
ls docs/design/ && cat docs/design/*/lld/<module>.md 2>/dev/null || true
```

### Step 3 — Write the LLD

Create `docs/design/<milestone>/lld/<module>.md` in your working directory.

**The LLD is a delta document, never a full system description.**

Before writing, determine your change scope by comparing the current requirements
against the previous-milestone LLD you read in Step 2:

- **No code changes in this module:** Write a minimal doc. State that the module is
  unchanged, explain in one paragraph why the existing code already handles this
  version's new requirements (e.g. "the `except CalcError` handler catches all new
  subclasses by inheritance"), and list only the new test cases added. Omit all
  sections about data structures, algorithms, and interfaces — refer the reader to
  the previous LLD for those. This is the correct, complete LLD for a no-change module.
- **Partial changes:** Write only the sections that changed. For sections that are
  identical to the prior version (e.g. TokenType variants that didn't change), omit
  them or write "Unchanged from v0.X.0 — see `docs/design/v0.X.0/lld/<module>.md`."
  Lead each changed section with what is new or different.
- **Significant rewrite or new module:** Write all sections as normal, but explicitly
  call out which parts are new vs. inherited from prior milestones.

**Common mistakes to avoid:**
- Do NOT copy-paste the full prior LLD and add a few lines at the bottom.
- Do NOT repeat unchanged interfaces, data structures, or algorithms.
- Do NOT write the LLD as if the prior version didn't exist.

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

### Step 4 — Follow the Mandatory Completion Steps

Follow the **Required Completion Steps** in your session prompt exactly:
- Step A: Commit the LLD doc
- Step B: `git push -u origin <branch>`
- Step C: `gh pr create ...`

Execute all three immediately without pausing.

### Step 5 — STOP

Do not merge. The orchestrator monitors the PR and merges when CI passes.

## Constraints

- **One PR, one doc**: this agent produces only `docs/design/<milestone>/lld/<module>.md`
- **Respect module scope**: only describe the module named in the session prompt
- **No implementation code**: this agent writes documentation only
- **Concrete recommendations**: every design decision must have a rationale,
  not just a list of options
- **No Agent tool**: do not spawn sub-agents or Explore agents under any circumstances
