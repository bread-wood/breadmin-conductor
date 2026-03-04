Start the research worker orchestrator for this repository in autonomous mode.

This command is specifically for **research tasks** — dispatching research agents, reviewing
completed research for follow-up topics, creating follow-up issues, and re-dispatching until
the **active milestone's** research queue is empty. This is distinct from the `issue-worker`
command which handles implementation tasks.

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

4. Read the project-level CLAUDE.md (if it exists) to understand project context.

## Parallelism Limit

**Maximum 5 concurrent research agents.** Do NOT launch more than 5 agents at a time.
Wait for completions before dispatching the next batch.

## Active Milestone Scoping

**Only dispatch issues assigned to the current active milestone(s).** Do not dispatch
issues from future milestones even if unblocked. The active milestone is determined by:
1. The argument to this command (e.g., `--milestone "M2"`) if provided
2. Otherwise, the lowest-numbered milestone that still has open research issues

If an issue has no milestone assigned, skip it — do not dispatch.

### Stopping Criteria

**Stop dispatching when the active milestone has no remaining unblocked, unassigned research
issues.** Do NOT generate more follow-ups just to keep the pipeline running. When the queue
drains:
1. Do a completeness check (Step 4)
2. Post a session report (Step 5)
3. STOP

## Research-Specific Rules

### Research Label
All research issues MUST have the `stage/research` label. Only process issues with this label.

### Follow-Up Issue Policy

Follow-up research issues exist to capture **genuinely new architectural questions** raised
by a doc. They do NOT exist to track:
- Implementation tasks (data collection, scripting, API lookups)
- Granular empirical measurements that belong inside a research doc
- Details already covered by an existing doc

**Follow-up issues must:**
- Be assigned the **same milestone** as the parent issue
- Represent a topic that warrants a standalone research document
- Not duplicate an existing open or closed issue
- Be worth a standalone research document — only file issues that would genuinely block design

### Research Agent Instructions

Research agents are dispatched with `Agent(isolation: "worktree")` and given:
- A specific research topic and detailed scope
- Instructions to write a comprehensive markdown document in `docs/research/<milestone>/`
- Instructions to include a **"Follow-Up Research Recommendations"** section
- Instructions to cross-reference existing research docs
- Instructions to flag contradictions with other docs
- **Instructions to create follow-up GitHub issues themselves**
- **Instructions to write a full PR body including follow-ups spawned**

## Execution Loop

Operate as a pipeline until the active milestone's research queue is empty:

### Step 1 — Survey & Select (no confirmation)

List all open research issues for the active milestone:
```bash
gh issue list --state open --label stage/research --assignee "" --limit 200 \
  --json number,title,labels,body,milestone
```

Filter to issues in the active milestone only. Parse dependency references
(`Depends on: #N`) from issue bodies. Identify unblocked issues (no open dependencies)
and rank them by:
1. **Downstream impact** — issues that unblock the most other issues go first
2. **Issue number** (lowest first) — tiebreaker; older issues first

Select up to 5 issues (parallelism limit). Log the selection — do NOT pause for confirmation.

### Step 2 — Claim & Dispatch

For each selected issue, sequentially:
- `gh issue edit <N> --add-assignee @me --add-label in-progress`
- Create an isolated git worktree for the agent:
  ```bash
  WORKTREE=.claude/worktrees/<N>-<short-slug>
  git worktree add $WORKTREE -b <N>-<short-slug> origin/$DEFAULT_BRANCH
  git -C $WORKTREE push -u origin <N>-<short-slug>
  ```
- The worktree path is passed to the agent as `Working Directory` in the prompt.

Launch sub-agents in parallel using `Agent(isolation: "worktree")`.

**Research agent prompt template:**

> You are implementing research issue #N on branch `<Branch>`.
> Your task: <research topic and scope from issue body>.
> Milestone: <milestone name>
> Allowed scope: `docs/research/<milestone>/`
>
> ## Working Directory
> Your isolated working directory is `<Working Directory>` (from Session Parameters).
> This directory is already a git checkout on branch `<Branch>`.
> ALL file reads, writes, and git operations must use this directory.
>
> Steps:
> 1. Change to your working directory: `cd <Working Directory>`
>    (This is a git worktree already on branch `<Branch>` — do NOT run `git checkout`.)
> 2. Read the issue in full: `gh issue view <N> --repo <owner>/<repo>`
> 3. Read all related existing research docs referenced in the issue body
>    (use absolute paths under `<Working Directory>/docs/research/<milestone>/`)
> 4. Read ALL existing docs in `<Working Directory>/docs/research/<milestone>/` to understand what's already covered
> 5. Research the topic extensively using web search
> 6. Write a comprehensive document at `<Working Directory>/docs/research/<milestone>/<NN>-<slug>.md`:
>    - MUST include a "Follow-Up Research Recommendations" section
>    - MUST include a "Sources" section with full citations
>    - MUST cross-reference related research docs
>    - MUST flag any contradictions with other docs
> 7. **Create follow-up GitHub issues** for each recommendation in the doc that:
>    - Represents a genuinely new architectural/research question
>    - Is NOT already covered by an existing issue (check: `gh issue list --repo <owner>/<repo> --state all --label stage/research --limit 300 --json number,title`)
>    - Would require a standalone research document to answer
>    - Tag each recommendation in the doc's "Follow-Up Research Recommendations" section:
>      - `[BLOCKS_IMPL]` — needed before the current implementation milestone can be designed
>      - `[DEFERRED]` — useful but doesn't block v1; belongs in a later research milestone
>      - `[WONT_RESEARCH]` — not worth a standalone doc; note inline, don't file an issue
>    - Only create GitHub issues for `[BLOCKS_IMPL]` and `[DEFERRED]` items
>    - To assign the milestone, inspect what milestones exist and their descriptions:
>      `gh milestone list --repo <owner>/<repo>`
>      Pick the lowest-numbered *research* milestone beyond the current one for `[DEFERRED]` items.
>      Use the current research milestone for `[BLOCKS_IMPL]` items.
>    - Use: `gh issue create --repo <owner>/<repo> --title "<topic>" --label "stage/research,<P0|P1|P2|P3>" --milestone "<resolved milestone name>" --body "..."`
>    - Body MUST include: `## Spawned From`, `## Research Areas`, `## Deliverable`, `## Dependencies`
>    - DO NOT create issues for: implementation tasks, data collection scripts, narrow empirical measurements, things already covered in existing docs
> 8. Commit from your working directory:
>    ```bash
>    cd <Working Directory>
>    git add docs/research/<milestone>/<NN>-<slug>.md
>    git commit -m "docs: add <topic> research (Closes #<N>) [skip ci]"
>    ```
> 9. Push: `git push -u origin <Branch>`
> 10. Create PR:
>    ```bash
>    gh pr create --repo <owner>/<repo> \
>      --title "docs: <topic> research (#<N>)" \
>      --label "stage/research" \
>      --body "Closes #<N>
>
>    ## Summary
>    <1-3 sentences describing findings>
>
>    ## Follow-up issues spawned
>    <list issue numbers or 'none'>
>    "
>    ```
> 11. Verify CI and reviews (REQUIRED):
>    Research commits use `[skip ci]` so CI should pass immediately.
>    Check that the PR is mergeable:
>      `gh pr view <PR-number> --repo <owner>/<repo> --json mergeable,mergeStateStatus --jq '{mergeable,mergeStateStatus}'`
>    If CHANGES_REQUESTED from a reviewer:
>      Collect feedback: `gh pr view <PR-number> --repo <owner>/<repo> --json reviews`
>      Address all feedback in ONE commit, push, re-request review.
>      Max 2 review fix attempts.
> 12. When CI passes + no CHANGES_REQUESTED outstanding:
>    Output exactly one line: `Done.`
>    Do NOT merge. The orchestrator handles merging.

### Step 3 — Merge & Requeue

As **each agent completes** (do not wait for the entire batch):

1. **Clean up** its worktree: `git worktree remove --force <path>`
2. **Find the PR** the agent created: `gh pr list --repo <owner>/<repo> --head <branch> --state open --json number`
   - The research agent already verified CI and reviews are clean before stopping.
   - The brimstone orchestrator squash-merges it: `gh pr merge <PR> --squash --delete-branch`
3. **Pull**: `git pull origin $DEFAULT_BRANCH`
4. **Verify follow-ups were created**: read the research doc's "Follow-Up Issues Spawned" section
   - If the agent created zero follow-ups, read the doc's "Follow-Up Recommendations" section yourself and decide if any warrant issues — if so, create them with `stage/research,<P0|P1|P2|P3>` labels
5. **Comment on the closed parent issue**: `gh issue comment <N> --repo <owner>/<repo> --body "Research doc merged in PR #<PR>. Follow-up issues: <list or 'none'>"`
6. **Re-survey**: check for newly unblocked issues in the active milestone
7. **Dispatch next batch** (up to 5 agents total active)
8. **Continue** until active milestone queue is empty AND all agents complete

### Step 4 — Completeness Check and Research Gate

When the pipeline drains for the active milestone:

1. **Blocking gap analysis**: For each remaining open research issue in this milestone, ask:
   *"Is there a specific implementation issue in the next milestone that cannot be designed
   without this answer?"*
   - YES → **blocking**: must dispatch before implementation begins; leave in current milestone
   - NO → **non-blocking**: migrate to the next research milestone now:
     ```bash
     gh issue edit <N> --milestone "<next research milestone>"
     ```

2. **Ready-to-implement verdict**: If zero blocking issues remain after step 1:
   - Declare: "Research milestone complete — ready to begin implementation."
   - Do NOT create more research issues to fill the queue.
   - The session report must include this verdict prominently.

3. **Cross-cutting gap check**: Are there *blocking* topics not yet covered by any issue?
   - If yes, file them as new issues in the current milestone — do NOT auto-dispatch.
   - Apply the `[BLOCKS_IMPL]` / `[DEFERRED]` tags before filing.
   - If no, leave it at that — a complete research milestone does not require every possible
     question to be answered.

### Step 5 — Session Report

1. **Print a terminal summary**:
   - Research issues completed (with PR numbers)
   - Follow-up issues created (with spawning doc)
   - Issues failed/abandoned (with reasons)
   - Remaining open research issues in active milestone
   - **Milestone completion status**: X of Y issues resolved

## Constraints

- **Maximum 5 parallel agents** — respect this limit at all times
- **Active milestone only** — never dispatch issues outside the current milestone
- **Agents create their own follow-ups** — orchestrator reviews quality, doesn't recreate from scratch
- **PRs must have full descriptions** — title, summary, key findings, follow-ups spawned
- **No infinite loops** — stop when the milestone queue is empty
- **Follow-ups are self-qualifying** — only file issues that pass the `[BLOCKS_IMPL]` filter; the agent is responsible for not filing low-value issues
- Report progress: log each completion/merge/follow-up creation as it happens
