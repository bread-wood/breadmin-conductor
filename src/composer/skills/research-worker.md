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
All research issues MUST have the `research` label. Only process issues with this label.

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
- Include `triage` in their label list so the orchestrator can find and score them quickly

**All follow-ups are subject to the triage rubric** (see CLAUDE.md "Research Issue Triage").
The orchestrator applies the rubric immediately after each merge — do not assume follow-ups
will be dispatched. Issues scoring < 2/3 are closed with `wont-research`.

### Research Agent Instructions

Research agents are dispatched with `Agent(isolation: "worktree")` and given:
- A specific research topic and detailed scope
- Instructions to write a comprehensive markdown document in `docs/research/`
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
gh issue list --state open --label research --assignee "" --limit 200 \
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
- `git checkout -b <N>-<short-slug> origin/$DEFAULT_BRANCH && git push -u origin <N>-<short-slug>`
- `git checkout $DEFAULT_BRANCH`

Launch sub-agents in parallel using `Agent(isolation: "worktree")`.

**Research agent prompt template:**

> You are implementing research issue #N on branch `<branch-name>`.
> Your task: <research topic and scope from issue body>.
> Milestone: <milestone name>
> Allowed scope: `docs/research/`
>
> Steps:
> 1. `git checkout <branch-name>`
> 2. Read the issue in full: `gh issue view <N>`
> 3. Read all related existing research docs referenced in the issue body
> 4. Read ALL existing docs in `docs/research/` to understand what's already covered
> 5. Research the topic extensively using web search
> 6. Write a comprehensive document at `docs/research/<NN>-<slug>.md`:
>    - MUST include a "Follow-Up Research Recommendations" section
>    - MUST include a "Sources" section with full citations
>    - MUST cross-reference related research docs
>    - MUST flag any contradictions with other docs
> 7. **Create follow-up GitHub issues** for each recommendation in the doc that:
>    - Represents a genuinely new architectural/research question
>    - Is NOT already covered by an existing issue (check: `gh issue list --state all --label research --limit 300 --json number,title`)
>    - Would require a standalone research document to answer
>    - Tag each recommendation in the doc's "Follow-Up Research Recommendations" section:
>      - `[BLOCKS_IMPL]` — needed before the current implementation milestone can be designed
>      - `[V2_RESEARCH]` — useful but doesn't block v1; belongs in a later research milestone
>      - `[WONT_RESEARCH]` — not worth a standalone doc; note inline, don't file an issue
>    - Only create GitHub issues for `[BLOCKS_IMPL]` and `[V2_RESEARCH]` items
>    - To assign the milestone, inspect what milestones exist and their descriptions:
>      `gh milestone list --repo <owner>/<repo>`
>      Pick the lowest-numbered *research* milestone beyond the current one for `[V2_RESEARCH]` items.
>      Use the current research milestone for `[BLOCKS_IMPL]` items.
>    - Use: `gh issue create --title "Research: <topic>" --label "research,triage" --milestone "<resolved milestone name>" --body "..."`
>    - Body MUST include: `## Spawned From`, `## Research Areas`, `## Deliverable`, `## Dependencies`
>    - DO NOT create issues for: implementation tasks, data collection scripts, narrow empirical measurements, things already covered in existing docs
>    - Add `triage` label to ALL follow-up issues — the orchestrator will score them; don't pre-filter, but be conservative
> 8. Commit with message referencing the issue: `git commit -m "docs: add <topic> research (Closes #<N>)"`
> 9. `git push -u origin <branch-name>`
> 10. Create PR with a full description:
>     ```
>     gh pr create \
>       --title "docs: add <descriptive title of what was researched>" \
>       --label "research" \
>       --body "$(cat <<'EOF'
>     ## Summary
>     <2-3 sentence summary of key findings>
>
>     ## Key Findings
>     - <bullet 1>
>     - <bullet 2>
>     - <bullet 3>
>
>     ## Follow-Up Issues Spawned
>     <list of issues created, or "None — existing issues cover all follow-up topics">
>
>     Closes #<N>
>     EOF
>     )"
>     ```
> 11. STOP. Do not merge. Do not comment on the parent issue (orchestrator does this after merge).

### Step 3 — Merge, Review & Requeue

As **each agent completes** (do not wait for the entire batch):

1. **Clean up** its worktree: `git worktree remove --force <path>`
2. **Merge the PR**: `gh pr merge <PR-number> --squash --delete-branch`
3. **Pull**: `git pull origin $DEFAULT_BRANCH`
4. **Verify follow-ups were created**: check PR body for "Follow-Up Issues Spawned" section
   - If the agent created follow-ups, apply the **triage rubric** to each one immediately:
     ```bash
     gh issue list --state open --label triage --limit 50
     ```
     For each `triage`-labelled follow-up, score it (3 questions, need ≥2 yes):
     1. Changes a current-milestone impl decision?
     2. Genuinely new, not covered by an existing doc or issue?
     3. Correctness or security risk if skipped?
     - **Score < 2**: `gh issue close <N> --reason "not planned" --comment "score X/3"` + add `wont-research`
     - **Score ≥ 2**: `gh issue edit <N> --remove-label triage` (keep, leave in queue)
   - If the agent created zero follow-ups, read the doc's "Follow-Up Recommendations" section yourself and decide if any warrant issues — if so, create them with `triage` label and score them
5. **Comment on the closed parent issue**: `gh issue comment <N> --body "Research doc merged in PR #<PR>. Follow-up issues: <list or 'none'>"`
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
   - Apply the triage rubric and the `[BLOCKS_IMPL]` / `[V2_RESEARCH]` tags before filing.
   - If no, leave it at that — a complete research milestone does not require every possible
     question to be answered.

### Step 5 — Session Report

1. **Print a terminal summary**:
   - Research issues completed (with PR numbers)
   - Follow-up issues created (with spawning doc)
   - Issues failed/abandoned (with reasons)
   - Remaining open research issues in active milestone
   - **Milestone completion status**: X of Y issues resolved

2. **File the next pipeline stage issue** (only if research is declared complete):
   ```bash
   gh issue create --title "Run design-worker for <milestone>" --label "pipeline" --milestone "<milestone>"
   ```

3. **Post a Notion report** under "CC Autonomous Coding Sessions"
   (parent page ID: `317bb275-6a02-803d-a59f-dc56c3527942`) using
   `mcp__notion__API-post-page` with:
   - **Title**: `Research Session — {YYYY-MM-DD} — {repo name}`
   - **Body**: research completed, follow-ups created, gaps identified, remaining work

## Constraints

- **Maximum 5 parallel agents** — respect this limit at all times
- **Active milestone only** — never dispatch issues outside the current milestone
- **Agents create their own follow-ups** — orchestrator reviews quality, doesn't recreate from scratch
- **PRs must have full descriptions** — title, summary, key findings, follow-ups spawned
- **No infinite loops** — stop when the milestone queue is empty
- **Triage every follow-up before dispatch** — apply rubric immediately after each merge; close failing issues with `wont-research`; remove `triage` label from passing issues
- **Post session report to Notion** when done
- Report progress: log each completion/merge/follow-up creation as it happens
