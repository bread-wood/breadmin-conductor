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

Build a set of normalized titles to skip during Step 3.

### Step 3 — Plan Implementation Issues

For each logical unit of work needed to implement what the design docs specify:

1. **Define scope** — which module boundary (from CLAUDE.md) does this unit touch?
   Each issue must be scoped to exactly one module from the module isolation table.
   One issue per logical unit per module.

2. **Write acceptance criteria** — concrete, testable, binary conditions for "done".
   Use "must" language only; avoid "should" and "might".
   At least two criteria per issue.

3. **Write test requirements** — what unit tests must exist, what integration tests (if any).
   Reference specific function or class names where the design doc names them.

4. **Identify dependencies** — which other impl issues (by title or anticipated number)
   must complete before this one can start?
   Check for circular dependencies before proceeding.

5. **Assign label** — use the appropriate `feat:*` label from CLAUDE.md:
   - `feat:config` — `src/brimstone/config.py`
   - `feat:runner` — `src/brimstone/runner.py`, `src/brimstone/session.py`
   - `feat:health` — `src/brimstone/health.py`
   - `feat:logging` — `src/brimstone/logger.py`
   - `feat:cli` — `src/brimstone/cli.py`, `src/brimstone/skills/`
   - `infra` — `pyproject.toml`, `.github/`, `CLAUDE.md`, `README.md`

6. **Skip if duplicate** — if a normalized version of the proposed title matches an existing
   issue title, log the skip and move on.

If `--dry-run` is set, print each planned issue (title, label, scope, criteria summary, deps)
to stdout and STOP — do not call `gh issue create`.

Otherwise, file each issue:
```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "<imperative verb phrase>" \
  --label "<feat:*>,stage/impl,P2" \
  --milestone "<milestone>" \
  --body "$(cat <<'EOF'
## Context
<1-2 sentences: what design doc section this implements and why it is needed>

## Acceptance Criteria
- [ ] <concrete, testable criterion 1>
- [ ] <concrete, testable criterion 2>
- [ ] ...

## Scope
Files to create or modify:
- `<path>` — <what changes>

## Test Requirements
- <what must be unit-tested>
- <what must be integration-tested (if any)>

## Dependencies
<Depends on: #N, or "None">

## Key Design Decisions
<Bullet list of non-obvious decisions made by the design docs, so the impl agent
does not re-litigate them. Reference the relevant LLD section if helpful.>
EOF
)"
```

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
