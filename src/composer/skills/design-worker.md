Start the design worker for this repository in autonomous mode.

The design worker translates completed research into HLD and LLD design documents.
It runs **after** research is declared complete (research-worker Step 4 verdict) and **before**
plan-issues begins. It produces no code and no GitHub issues — only design documents.

## Target Repository

The `--repo` argument controls which repository this worker operates on:

| Invocation | Behaviour |
|---|---|
| *(no flag)* | Operate on the current working directory. Fails if cwd is not a git repo. |
| `--repo owner/name` | Operate on an existing remote GitHub repo. All `gh` commands use `--repo owner/name`. |
| `--repo name` | Scaffold a new private GitHub repo named `name`, then operate on it. |
| `--repo path/to/local/dir` | Operate on the local directory. Fails if it is not a git repo. |

All `gh` commands in this skill must be scoped with `--repo <owner>/<name>` when the target is a remote repo.
All `git` commands must be run with `-C <local_path>` (or inside the cloned directory) when operating on a local path.

## Setup

1. Detect the default branch:
   ```bash
   DEFAULT_BRANCH=$(gh repo view --repo <owner>/<repo> --json defaultBranchRef --jq '.defaultBranchRef.name')
   git -C <local_path> checkout $DEFAULT_BRANCH && git -C <local_path> pull origin $DEFAULT_BRANCH
   ```

2. Run startup checks per the Orchestrator-Dispatch Protocol in ~/.claude/CLAUDE.md.

3. Read the project-level CLAUDE.md to understand module scope and pipeline model.

## Inputs

The design worker requires:
- `--research-milestone` — the research milestone whose docs to translate
- `--repo` — the target repository (optional; defaults to cwd)

## Execution

### Step 1 — Read All Research Docs

```bash
gh issue list --state closed --label research --milestone "<research-milestone>" \
  --json number,title --limit 200
```

For each closed research issue, find and read its doc in `docs/research/`:
```bash
ls docs/research/
```

Read every doc that corresponds to the research milestone. Build a mental model of:
- What is now known (key findings, constraints, chosen approaches)
- What was explicitly deferred or marked `[V2_RESEARCH]`
- Any blocking contradictions or unresolved `[INFERRED]` claims that affect design

### Step 2 — Audit Existing Design Docs

Check what design docs already exist to avoid recreating docs that are already up-to-date:
```bash
ls docs/design/
ls docs/design/lld/
```

### Step 3 — Write HLD (if not present or outdated)

If `docs/design/HLD.md` doesn't exist or is outdated relative to the research findings:

1. Create `docs/design/HLD.md` with:
   - **System overview** — one-paragraph description and pipeline stage table
   - **Component map** — ASCII diagram and module responsibility table with dependency edges
   - **Execution model** — stateless subprocess chaining, agent isolation via git worktrees, end-to-end sequence diagrams
   - **Error taxonomy** — `result` event schema, error classification table, retry policy
   - **Security architecture** — four-layer defense-in-depth (input sanitization, env isolation, tool permission policy, OS process isolation)
   - **Observability model** — three log streams, cost accounting schema, conductor log event list
   - **Constraints** — hard constraints (non-negotiable), rate limit constraints, soft constraints

2. Commit:
   ```bash
   git add docs/design/HLD.md
   git commit -m "docs: add HLD for <research-milestone>"
   ```

3. Push and open PR:
   ```bash
   git push -u origin <branch-name>
   gh pr create \
     --title "docs: HLD for <research-milestone>" \
     --label "build" \
     --milestone "<research-milestone>"
   ```

4. Wait for CI to pass; merge:
   ```bash
   gh pr checks <pr-number> --watch
   gh pr merge <pr-number> --squash --delete-branch
   git checkout $DEFAULT_BRANCH && git pull origin $DEFAULT_BRANCH
   ```

5. Continue to LLDs.

### Step 4 — Write LLD Per Module

For each module in the CLAUDE.md module isolation table that needs a design doc:

1. Read the module's scope from CLAUDE.md (which source files it covers).

2. Create a new branch for this LLD:
   ```bash
   git checkout -b lld-<module>-<research-milestone-slug> origin/$DEFAULT_BRANCH
   ```

3. Write `docs/design/lld/<module>.md` with:
   - **Module overview** — one paragraph; what problem it solves; which entry points or workers use it
   - **File paths and exports** — table of source files, their kinds (Python module, skill prompt, etc.), and what each exports or provides
   - **Public interface** — function signatures with type hints, dataclass schemas, class definitions; no implementation detail
   - **Data flows and sequence diagrams** — ASCII text-based diagrams showing how data moves through the module; include subprocess invocation patterns where relevant
   - **Error handling** — which errors this module classifies, what it raises, what callers must handle
   - **Test requirements** — what unit tests must exist, what integration tests are needed, what must be mocked
   - **Constraints and non-goals** — what this module explicitly does not do; hard constraints from research findings

4. Commit:
   ```bash
   git add docs/design/lld/<module>.md
   git commit -m "docs: add LLD for <module> (<research-milestone>)"
   ```

5. Push and open PR:
   ```bash
   git push -u origin lld-<module>-<research-milestone-slug>
   gh pr create \
     --title "docs: LLD for <module> (<research-milestone>)" \
     --label "build" \
     --milestone "<research-milestone>"
   ```

6. Wait for CI to pass; merge before writing the next LLD:
   ```bash
   gh pr checks <pr-number> --watch
   gh pr merge <pr-number> --squash --delete-branch
   git checkout $DEFAULT_BRANCH && git pull origin $DEFAULT_BRANCH
   ```

Repeat for each module that requires a design doc.

### Step 5 — Report

Print a summary:
- Research milestone processed
- Design docs produced (HLD + LLD list)
- Any research gaps that prevented full design coverage (note inline; do not file new issues)
- **Verdict**: "Ready for plan-issues — HLD and N LLD docs merged."

File the next pipeline stage issue:
```bash
gh issue create \
  --title "Run plan-issues for <research-milestone>" \
  --label "pipeline" \
  --milestone "<research-milestone>"
```

Post a Notion report under "CC Autonomous Coding Sessions"
(parent page ID: `317bb275-6a02-803d-a59f-dc56c3527942`) with:
- **Title**: `Design Session — {YYYY-MM-DD} — {repo name}`
- **Body**: research milestone processed, design docs produced, coverage gaps, verdict

## Constraints

- **No code** — design-worker creates design documents only, never touches source files
- **No GitHub issues** — creating impl issues is plan-issues's job; design-worker MUST NOT file impl issues
- **No dispatching** — design-worker does not launch sub-agents
- **One PR per doc** — HLD gets its own PR; each LLD gets its own PR; merge before writing the next
- **No gold-plating** — document only what research explicitly determined; don't invent scope
- **Post session report to Notion** when done
