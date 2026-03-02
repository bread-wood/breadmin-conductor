Start the issue worker orchestrator for this repository in autonomous mode.

## Setup

1. Detect the default branch:
   ```bash
   DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')
   ```

2. Ensure you're on the default branch:
   ```bash
   git checkout $DEFAULT_BRANCH && git pull origin $DEFAULT_BRANCH
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

2. **Post a Notion report** under "CC Autonomous Coding Sessions"
   (parent page ID: `317bb275-6a02-803d-a59f-dc56c3527942`) using
   `mcp__notion__API-post-page` with:
   - **Title**: `Session Report — {YYYY-MM-DD} — {repo name}`
   - **Body** (as Notion blocks): issues attempted, PRs merged, issues
     failed/abandoned, newly unblocked issues for next run, and total duration

## Constraints

- **Operate autonomously** — do not ask for confirmation before dispatching work
  (only ask during startup if orphaned work is detected)
- **Fill freed module slots immediately** as agents complete — maintain maximum throughput
- **Post session report to Notion** when the pipeline drains
- Maximize parallelism: dispatch as many non-conflicting agents as possible
- Respect module isolation: only one agent per module/package scope at a time
- Serialize merges: merge one PR at a time, rebase others if needed
- Report progress: log each completion/merge/dispatch as it happens
