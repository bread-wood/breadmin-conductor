Start the scope worker for this repository in autonomous mode.

The scope worker reads the completed design documents (HLD and per-module LLDs)
and produces fully-specified GitHub implementation issues with acceptance criteria, file scope,
test requirements, and a verified dependency graph. It runs **after** design-worker has merged
all design docs and **before** impl-worker begins. It creates GitHub issues only — no code,
no design docs, no source file changes.

## Setup

The orchestrator has already cloned the repository to a local path specified in the
`Local repo clone` session parameter. Use that path for all file reads.

1. Read the project-level CLAUDE.md to understand module scope and labels:
   ```bash
   cat <local_repo_clone>/CLAUDE.md
   ```

## Inputs

The scope worker requires:
- `Repository` — the target repository in `owner/repo` format
- `Milestone` — the milestone to file impl issues against (e.g. `v0.1.0`)
- `Local repo clone` — absolute path to the pre-cloned repository

## Execution

### Step 1 — Read Design Documents

**The design documents are the sole source of truth for scoping. Do not read research
issues, design issues, or any other issue history when planning impl issues.**

Design docs live under `<local_repo_clone>/docs/design/<milestone>/`.
Read them directly from the local filesystem — do NOT use `gh api` to fetch them.

Read the high-level design document:
```bash
cat <local_repo_clone>/docs/design/<milestone>/HLD.md
```

List and read all low-level design documents:
```bash
ls <local_repo_clone>/docs/design/<milestone>/lld/
```
For each file in `docs/design/<milestone>/lld/`, read it in full:
```bash
cat <local_repo_clone>/docs/design/<milestone>/lld/<module>.md
```

Build a complete picture of:
- Every module's responsibility and file scope
- Key design decisions already made (do not re-litigate)
- Interfaces between modules
- Any explicit implementation notes or acceptance hints in the docs
- Items explicitly deferred or marked out of scope for this version

### Step 2 — Audit Existing Impl Issues

Check only what `stage/impl` issues already exist for the milestone — to avoid duplication.
**Do not look at any other issues (research, design, pipeline, etc.).**

```bash
gh issue list --state open --milestone "<milestone>" --label "stage/impl" --limit 200 \
  --json number,title,labels,body --repo <owner>/<repo>
```

Also check closed `stage/impl` issues in case some were previously filed and closed in error:
```bash
gh issue list --state closed --milestone "<milestone>" --label "stage/impl" --limit 200 \
  --json number,title,labels --repo <owner>/<repo>
```

Build **two** skip sets from all returned issues (open + closed):

1. **Normalized titles** — lowercase the title, strip all punctuation and the milestone
   suffix (e.g. `" (v0.3.0)"`), collapse whitespace. If a proposed title normalizes to
   the same string as an existing issue, skip it.

2. **Covered modules** — extract the module name from each existing issue title.
   Any word that matches a module name from the LLD list (e.g. `lexer`, `parser`,
   `evaluator`, `errors`, `cli`) counts as "covered".
   If the module you are about to file an issue for is already covered by any existing
   `stage/impl` issue (regardless of title format), **skip it entirely**.

A proposed issue is skipped if **either** condition matches.
This prevents duplicate module issues even when title phrasing differs between runs.

### Step 3 — File Scaffold Issue First (initial milestone only)

**First, check whether the project is already bootstrapped:**
```bash
gh api repos/<owner>/<repo>/contents/pyproject.toml 2>/dev/null && echo EXISTS || echo MISSING
```

If `pyproject.toml` **already exists** on the default branch, the project is already
scaffolded from a prior milestone. **Skip this entire step** — do not file a scaffold
issue, do not create scaffold dependencies. Proceed directly to Step 3b.

If `pyproject.toml` is **missing**, file a scaffold issue as the very first issue.
All other impl issues must depend on it.

**Why:** Impl workers run in parallel. If each worker independently creates `pyproject.toml`
or `src/<pkg>/__init__.py`, every PR will have merge conflicts. The scaffold issue claims
these shared files so parallel workers never touch them. On subsequent milestones the
scaffold already exists — filing it again wastes an agent run and produces a no-op PR.

File the scaffold issue:
```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "impl: project scaffold — pyproject.toml, package init, CI baseline" \
  --label "infra,stage/impl,P1" \
  --milestone "<milestone>" \
  --body "$(cat <<'EOF'
## Context
Establishes the project scaffold so parallel module impl workers never conflict on shared files.
Must merge before any other impl issue begins.

## Acceptance Criteria
- [ ] `pyproject.toml` exists with correct package name, dependencies, and dev extras
- [ ] `src/<pkg>/__init__.py` exists (may be empty)
- [ ] `Makefile` (or equivalent) exists with `test` and `lint` targets
- [ ] `make test && make lint` pass on a clean checkout
- [ ] README exists with package name, one-line description, and basic usage

## Scope
Files to create or modify:
- `pyproject.toml` — package metadata, dependencies, dev tooling
- `src/<pkg>/__init__.py` — package init
- `Makefile` — test and lint targets
- `README.md` — minimal project readme
- `.github/` (if CI not yet configured)

## Test Requirements
- `make test` exits 0
- `make lint` exits 0

## Dependencies
None — this is the foundation issue.

## Key Design Decisions
- Parallel impl workers are forbidden from touching `pyproject.toml` or `src/<pkg>/__init__.py`
  after this issue merges; they add dependencies or exports to these files only via their own
  module-scoped files.
EOF
)"
```

Record the scaffold issue number. **Every other impl issue filed in Step 3b must include
`Depends on: #<scaffold-issue>` in its body.**

If Step 3 was skipped (project already bootstrapped), do **not** add scaffold dependencies
to any issue — there is no scaffold issue for this milestone.

### Step 3b — Plan Module Implementation Issues

For each logical unit of work needed to implement what the design docs specify:

1. **Define scope** — which module boundary (from CLAUDE.md) does this unit touch?
   Each issue must be scoped to exactly one module from the module isolation table.
   One issue per logical unit per module.

2. **Write acceptance criteria** — 2–4 binary, observable conditions for "done".
   These describe external behaviour changes, not implementation steps.
   Do NOT copy test cases, diffs, or function signatures from the LLD.

3. **Identify dependencies** — which other impl issues (by title or anticipated number)
   must complete before this one can start?
   **If a scaffold issue was filed in Step 3, every module issue must depend on it.**
   If Step 3 was skipped (project already bootstrapped), do not add scaffold dependencies.
   Check for circular dependencies before proceeding.

4. **Assign label** — use the appropriate `feat:*` label from CLAUDE.md:
   - `feat:config` — `src/brimstone/config.py`
   - `feat:runner` — `src/brimstone/runner.py`, `src/brimstone/session.py`
   - `feat:health` — `src/brimstone/health.py`
   - `feat:logging` — `src/brimstone/logger.py`
   - `feat:cli` — `src/brimstone/cli.py`, `src/brimstone/skills/`
   - `infra` — `pyproject.toml`, `.github/`, `CLAUDE.md`, `README.md`

5. **Skip if duplicate** — skip the issue if ANY of the following is true:
   - A normalized version of the proposed title matches an existing issue title.
   - The module this issue targets is already covered by any existing `stage/impl`
     issue for this milestone (check the "covered modules" set from Step 2).
   Log the skip reason and move on. **Never file two issues for the same module.**

If `--dry-run` is set, print each planned issue (title, label, scope, criteria summary, deps)
to stdout and STOP — do not call `gh issue create`.

Otherwise, file each issue:
```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "impl: <module> — <one-line summary of what changes>" \
  --label "<feat:*>,stage/impl,P2" \
  --milestone "<milestone>" \
  --body "$(cat <<'EOF'
## Module
`<module>` — `<primary file(s)>`

## LLD Reference
`docs/design/<milestone>/lld/<module>.md`
The impl agent must read this document in full before writing any code.

## Acceptance Criteria
- [ ] <binary, testable criterion — what "done" looks like from the outside>
- [ ] <one criterion per observable behaviour change; 2–4 total>
- [ ] All existing tests pass; new tests added per the LLD test strategy

## Dependencies
<Depends on: #N, or "None">
EOF
)"
```

**Do not include** diffs, code snippets, test cases, or step-by-step implementation notes.
The impl agent reads the LLD directly — the issue body is routing metadata, not a spec.

Record each filed issue number for the dependency step.

### Step 4 — Set Dependency Order

After all issues are filed, build a directed acyclic graph (DAG) from the dependency
relationships you identified in Step 3. You now have the real issue numbers for every issue.

Check for cycles:
- If a cycle exists, identify the least critical dependency edge and remove it (update the
  affected issue body with a comment explaining the removal).
- Cycles must be resolved before proceeding.

Compute the topological execution order:
```
#N1 (no deps) -> #N2 (depends on #N1) -> #N3 (depends on #N1, #N2) -> ...
```

If any issue has a dependency on an issue number that was not filed in this session
(i.e., it references a pre-existing open issue), verify that issue is open and in the
impl milestone. If it is closed or missing, remove the dependency and note the correction.

**Wire up GitHub dependency links for every dependency relationship.**
For each issue N that depends on issue M, append `Depends on: #M` to issue N's body:
```bash
gh issue edit <N> --repo <owner>/<repo> \
  --body "$(gh issue view <N> --repo <owner>/<repo> --json body --jq .body)

Depends on: #<M>"
```

Do this for **every dependency edge** in the DAG — one `gh issue edit` call per blocked issue.
brimstone's `_filter_unblocked` reads `Depends on: #N` from issue bodies to enforce execution
order — if these lines are absent or contain wrong numbers, all issues will be dispatched in
parallel regardless of order.

### Step 5 — Print Summary

Print a full summary to stdout:
- Milestone name
- Total issues filed (number, title, label, deps for each)
- Topological execution order
- Any design gaps that prevented full specification (note these as potential follow-up
  research or design issues — do NOT auto-file them)
- **Verdict**: "Ready for impl-worker — N issues filed, dependency graph complete."

## Constraints

- **GitHub issues only** — scope-worker creates GitHub issues only; never touches source files,
  design docs, or any file in the repository
- **No sub-agents** — scope-worker does not launch sub-agents; all work happens in this session
- **Spec first** — every issue must have acceptance criteria and scope before being filed
- **One issue per logical unit per module** — split by module boundary (see CLAUDE.md module
  isolation table); do not create cross-module issues
- **No gold-plating** — spec only what the design docs explicitly determine; do not invent
  scope or add features the docs do not mention
- **`--dry-run` prints, does not create** — if `--dry-run` is set, print all planned issues
  to stdout and stop without calling `gh issue create`
- **Verify no circular deps** before finalizing the issue set
