Start the issue worker orchestrator for this repository in autonomous mode.

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
   ```

2. Ensure you're on the default branch:
   ```bash
   git -C <local_path> checkout $DEFAULT_BRANCH && git -C <local_path> pull origin $DEFAULT_BRANCH
   ```

3. Run startup checks per the Orchestrator-Dispatch Protocol in ~/.claude/CLAUDE.md:
   - Check for active worktrees: `git worktree list`
   - Check for in-progress issues: `gh issue list --state open --label in-progress`
   - Check for open PRs: `gh pr list --state open`
   - If orphaned work is detected (stale worktrees, in-progress issues without PRs),
     ask the user before cleaning up — this is the only confirmation gate.
   - If no issues detected, proceed automatically.

4. Read the project-level CLAUDE.md (if it exists) to understand:
   - Module isolation rules and scope constraints
   - Dependency ordering between issues
   - Which packages/modules exist
   - Testing and linting commands

## Execution Loop

Operate as a continuous pipeline until no dispatchable work remains:

### Step 1 — Survey & Select (no confirmation)

List all open **non-research** issues with
`gh issue list --state open --assignee "" --limit 200 --json number,title,labels,body`.
**Skip issues labeled `research`** — those are handled by the `/research-worker` command.
Parse dependency references (`Depends on: #N`) from issue bodies. Identify unblocked
issues (no open dependencies) and rank them by:
1. **Downstream impact** (highest first) — how many blocked issues does completing
   this issue transitively unblock? Issues that gate the most work go first.
2. **Bug fixes** — issues labeled `bug` take priority over features at equal impact.
3. **Issue number** (lowest first) — tiebreaker; older issues first.

Select the highest-priority unblocked issue per module (respecting module isolation —
one agent per module/scope at a time). Log the selection as an FYI message —
do NOT pause for user confirmation.

### Step 2 — Claim & Dispatch

For each selected issue, sequentially:
- `gh issue edit <N> --add-assignee @me --add-label in-progress`
- `git checkout -b <N>-<short-slug> origin/$DEFAULT_BRANCH && git push -u origin <N>-<short-slug>`
- `git checkout $DEFAULT_BRANCH`

Launch sub-agents in parallel using `Agent(isolation: "worktree")`.
Follow the Sub-Agent Instructions Template from ~/.claude/CLAUDE.md.

Include the following test generation instructions in every sub-agent prompt:

### Test Generation (required for all impl agents)

**Before writing implementation**, identify tests needed from the LLD acceptance criteria
in the issue. Write test stubs first; they define the expected interface.

Cover all applicable tiers:

| Tier | When | File |
|------|------|------|
| **Unit** | Always — one test per new public function/method | `tests/unit/test_<module>.py` |
| **Integration** | When the change spans module boundaries (e.g. runner → logger) | `tests/integration/test_<module>.py` |
| **Smoke** | When CLI entry points are added or changed | `tests/smoke/test_cli.py` |
| **E2E** | When the issue affects full pipeline behavior | `tests/e2e/` |

Rules:
1. **Test-first for new public functions** — write the stub before the implementation.
2. **No untested public functions** — every new public function needs at least one test.
   Do not create the PR until this is true.
3. **Derive tests from the LLD** — implement the test cases the design specifies;
   do not invent coverage for things not in scope.
4. **Naming**: test files match `test_<module>.py`; test functions match `test_<what_it_does>`.

### Step 3 — Monitor & Continue

As **each agent completes** (do not wait for the entire batch):

1. **Clean up** its worktree.
2. **Evaluate the result**:
   - If the agent created a PR:
     - Check CI: `gh pr checks <PR-number> --watch`
     - Check review: `gh pr view <PR-number> --json reviews`
     - If CI + review pass → squash merge:
       ```bash
       gh pr merge <PR-number> --squash --delete-branch
       git pull origin $DEFAULT_BRANCH
       ```
     - If CI or review fails → attempt one fix cycle (rebase, address feedback,
       push, re-check). If still failing after retry → abandon the issue:
       ```bash
       gh issue edit <N> --remove-assignee @me --remove-label in-progress
       gh pr close <PR-number>
       git push origin --delete <branch-name>
       ```
   - If the agent failed to create a PR → abandon the issue (unclaim, delete branch).

3. **Re-survey**: Check for newly unblocked issues (dependencies may have been
   resolved by the merge just completed).

4. **Fill freed slots**: If a module slot is now free AND unblocked work exists
   for that module → claim and dispatch a new agent immediately (repeat Step 2
   for just that issue).

5. **Continue** until all agents have completed AND no new dispatchable work remains.

### Step 4 — Session Report

When the pipeline is fully drained (no active agents, no remaining dispatchable work):

1. **Print a terminal summary** listing:
   - Issues attempted
   - PRs merged (with PR numbers)
   - Issues failed/abandoned (with reasons)
   - Newly unblocked issues available for the next run

2. **File the next pipeline stage issue** — kick off planning for the next version:
   ```bash
   gh issue create \
     --title "Run plan-milestones for <next version>" \
     --label "pipeline"
   ```

## Constraints

- **Operate autonomously** — do not ask for confirmation before dispatching work
  (only ask during startup if orphaned work is detected)
- **Fill freed module slots immediately** as agents complete — maintain maximum throughput
- Maximize parallelism: dispatch as many non-conflicting agents as possible
- Respect module isolation: only one agent per module/package scope at a time
- Serialize merges: merge one PR at a time, rebase others if needed
- Report progress: log each completion/merge/dispatch as it happens
