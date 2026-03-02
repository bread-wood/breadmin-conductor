# LLD: CLI Module and Skills

**Module:** `cli` + `skills/`
**Files:** `src/composer/cli.py`, `src/composer/skills/`
**Issue:** #114
**Status:** Draft
**Date:** 2026-03-02

---

## 1. Module Overview

`cli.py` is the entry-point layer for all four composer commands. It owns the Click command definitions, flag wiring, startup sequence, worker loop orchestration, skill injection, and post-run reporting. It imports from `config`, `health`, `runner`, `session`, and `logger`; it never contains business logic that belongs to those modules.

`skills/` is a collection of markdown files that serve as the prompt blueprints for each worker. At dispatch time `cli.py` reads the appropriate skill file, renders runtime substitutions into it, and passes the rendered string as the `-p` prompt to `runner.run()`. Skills contain the authoritative step-by-step instructions that the headless Claude Code subprocess follows.

**File paths and exports:**

| File | Kind | Exports / Purpose |
|------|------|-------------------|
| `src/composer/cli.py` | Python module | Click group `composer`; Click commands `impl_worker`, `research_worker`, `design_worker`; `inject_skill(skill_name, base_prompt) -> str`; `UsageGovernor` class |
| `src/composer/skills/research-worker.md` | Skill prompt | Headless research-worker orchestrator instructions |
| `src/composer/skills/impl-worker.md` | Skill prompt | Headless implementation-worker orchestrator instructions |
| `src/composer/skills/design-worker.md` | Skill prompt | Headless design-worker instructions (no sub-agents) |
| `src/composer/skills/plan-milestones.md` | Skill prompt | Milestone planning instructions (interactive / first-time setup) |

---

## 2. Entry Point Map

| Entry point | CLI command | Click command | Worker function | Key flags |
|-------------|-------------|---------------|-----------------|-----------|
| `impl-worker` | `impl-worker` | `impl_worker()` | `_run_impl_worker()` | `--repo` (required), `--milestone`, `--model`, `--max-budget`, `--max-turns`, `--dry-run`, `--resume` |
| `research-worker` | `research-worker` | `research_worker()` | `_run_research_worker()` | `--repo` (required), `--milestone` (required), `--model`, `--max-budget`, `--max-turns`, `--dry-run`, `--resume` |
| `design-worker` | `design-worker` | `design_worker()` | `_run_design_worker()` | `--repo` (required), `--research-milestone` (required), `--model`, `--dry-run` |
| `composer health` | `composer health` | `health()` | inline | `--repo` |
| `composer cost` | `composer cost` | `cost()` | inline | `--repo`, `--stage` |

**Flag semantics:**

- `--repo`: `OWNER/REPO` string passed through to the runner subprocess environment and embedded in the skill prompt. Required for all workers.
- `--milestone`: Scopes which GitHub milestone the worker processes. For `research-worker` this is required; for `impl-worker` it defaults to auto-detecting the lowest-numbered open impl milestone.
- `--research-milestone`: For `design-worker`; names the *completed* research milestone whose docs are to be translated into impl issues.
- `--model`: Overrides `Config.model`. CLI flag takes precedence over env var, which takes precedence over default (`claude-sonnet-4-6`).
- `--max-budget`: USD cap forwarded to `claude -p --max-budget-usd`. Effective only in API key auth mode; has no effect on subscription sessions (see `docs/research/08-usage-scheduling.md §6`).
- `--max-turns`: Overrides `Config.max_turns`. Forwarded to `claude -p --max-turns`.
- `--dry-run`: Renders the full invocation (prompt + flags) and prints it to stdout without executing `runner.run()`.
- `--resume`: Run ID (UUID) of a previous checkpoint to resume. Worker validates that the checkpoint's `run_id` matches before proceeding.

---

## 3. Startup Sequence (All Workers)

Every worker (`impl-worker`, `research-worker`, `design-worker`) runs the following steps before entering its main loop. Steps are synchronous and must all succeed before any agent is dispatched.

```
1. RECURSIVE INVOCATION GUARD
   if os.environ.get("CLAUDECODE") == "1":
       print("Error: composer cannot be invoked from within a Claude Code session.")
       sys.exit(1)
   # Prevents conductor from accidentally launching itself inside a sub-agent.

2. LOAD CONFIG
   config = Config(
       model=model or None,        # CLI flag overrides env var
       max_budget=max_budget or None,
       max_turns=max_turns or None,
   )
   # Pydantic resolves: CLI flag > CONDUCTOR_* env var > default

3. RUN HEALTH CHECKS
   report = health.check_all(repo=repo, config=config)
   if report.has_fatal:
       for item in report.items:
           print(item.format())
       sys.exit(1)
   # health.check_all() verifies: `claude` binary exists, `gh` binary exists,
   # `git` binary exists, repo is accessible, auth mode is detectable.
   # Non-fatal warnings are printed but do not abort.

4. LOAD OR CREATE CHECKPOINT
   if resume:
       checkpoint = session.load(run_id=resume, config=config)
       if checkpoint is None:
           print(f"Error: no checkpoint found for run_id={resume}")
           sys.exit(1)
   else:
       checkpoint = session.new(config=config)
   # checkpoint carries: run_id (UUID), milestone, claimed_issues, completed_issues,
   # backoff_until, worker_type.

5. VALIDATE RESUME RUN_ID (only if --resume)
   if resume and checkpoint.run_id != resume:
       print(f"Error: checkpoint run_id mismatch ({checkpoint.run_id} != {resume})")
       sys.exit(1)
   # Guards against accidentally resuming the wrong checkpoint.

6. LOG stage_start EVENT
   log_conductor_event(
       run_id=checkpoint.run_id,
       phase="init",
       event_type="stage_start",
       payload={
           "worker_type": WORKER_TYPE,    # "research" | "implementation" | "design"
           "milestone": milestone,
           "issue_count": <open_issues_in_milestone>,
       },
       data_dir=config.data_dir,
   )

7. ENTER WORKER LOOP
   # Control passes to the worker-specific loop defined in Sections 4-6.
```

**Environment passed to all `claude -p` subprocesses:**

```python
subprocess_env = {
    "HOME": os.environ["HOME"],
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "NOTION_TOKEN": os.environ.get("NOTION_TOKEN", ""),
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_TELEMETRY": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    # No CLAUDE_CONFIG_DIR — orchestrator sessions inherit user config for Notion MCP.
    # Sub-agent worker processes set CLAUDE_CONFIG_DIR to an isolated temp dir.
}
# ANTHROPIC_API_KEY is forwarded only if present (API key auth mode).
if "ANTHROPIC_API_KEY" in os.environ:
    subprocess_env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
```

---

## 4. Research Worker Loop

The research worker loop is entered after the startup sequence (Section 3) completes. It processes one active research milestone to completion, dispatching up to `CONCURRENCY_LIMIT` research agents in parallel.

### 4.1 Constants

```
CONCURRENCY_LIMIT = governor.max_concurrency   # tier-dependent; see Section 8
MAX_RETRIES = 3                                # per-issue retry cap
RESEARCH_LABELS = "research"
TRIAGE_LABEL = "triage"
```

### 4.2 Main Loop Pseudocode

```
LOOP:

  STEP 1 — CHECK BACKOFF STATE
    if governor.backoff_until is not None and datetime.utcnow() < governor.backoff_until:
        sleep until governor.backoff_until
        run probe: runner.run("OK", max_turns=1, ...)  # cheap health check
        if probe fails: extend backoff by 30 minutes; loop to STEP 1

  STEP 2 — SELECT NEXT BATCH OF ISSUES
    issues = gh_list_open_research_issues(
        milestone=milestone,
        label=RESEARCH_LABELS,
        assignee="",           # unassigned only
        exclude_label="in-progress",
        limit=200,
    )
    # Parse "Depends on: #N" from each issue body.
    # Remove issues whose dependencies include any open issue.
    unblocked = filter_unblocked(issues)
    # Sort: highest downstream impact first, then lowest issue number.
    ranked = sort_by_impact_then_number(unblocked)
    # Take up to (CONCURRENCY_LIMIT - governor.active_count) issues.
    batch = ranked[: CONCURRENCY_LIMIT - governor.active_count]

    if not batch:
        if governor.active_count == 0:
            go to COMPLETION GATE  # no issues remain; time to check gate
        else:
            wait for any active agent to complete; continue LOOP

  STEP 3 — CLAIM AND DISPATCH (sequential claim, parallel dispatch)
    for issue in batch:  # sequential — prevents claim races
        a. body = sanitize(issue.body)
           # strip shell metacharacters: remove $, `, \, unmatched quotes
           # truncate to MAX_PROMPT_CHARS (16,000) with a suffix marker if truncated

        b. gh issue edit <issue.number> --add-assignee @me --add-label in-progress
           log_conductor_event(phase="claim", event_type="issue_claimed",
                               payload={"issue_number": issue.number, "branch": branch_name})

        c. prompt = inject_skill("research-worker",
               f"## Session Parameters\n"
               f"- Repository: {repo}\n"
               f"- Active Milestone: {milestone}\n"
               f"- Issue: #{issue.number} — {issue.title}\n"
               f"- Session Date: {today}\n\n"
               + body)

        d. checkpoint.record_dispatch(issue.number)
           session.save(checkpoint, config)

    dispatch all issues in batch in parallel:
        results = asyncio.gather(*[
            runner.run(
                prompt=prompts[issue.number],
                allowed_tools=[
                    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
                    "WebSearch", "WebFetch",
                    "mcp__notion__API-post-page",
                ],
                max_turns=config.max_turns,
                max_budget=config.max_budget if api_key_mode else None,
                model=config.model,
                env=subprocess_env,
                mcp_config=NOTION_MCP_CONFIG_PATH,
                log_context=LogContext(run_id=checkpoint.run_id, repo=repo,
                                       stage="research", issue_number=issue.number),
                data_dir=config.data_dir,
            )
            for issue in batch
        ])

  STEP 4 — HANDLE RESULTS (as each completes)
    for (issue, result) in zip(batch, results):

        governor.record_result(result)   # update token totals

        if result.subtype == "success":
            # Triage any follow-up issues the agent created
            triage_issues = gh_list_issues(label=TRIAGE_LABEL, state="open")
            for fi in triage_issues:
                score = triage_rubric(fi)      # 3-question rubric from CLAUDE.md
                if score < 2:
                    gh_close_issue(fi.number, reason="not planned",
                                   comment=f"score {score}/3 — below threshold")
                    gh_add_label(fi.number, "wont-research")
                else:
                    gh_remove_label(fi.number, TRIAGE_LABEL)
                    # Issue stays open; will be picked up in a future batch

        elif result.subtype in ("error_max_turns", "error_during_execution"):
            # Check if this looks like a rate-limit error
            if is_rate_limit_error(result):
                gh issue edit <issue.number> --remove-assignee @me --remove-label in-progress
                checkpoint.defer(issue.number)
                governor.record_429()
                log_conductor_event(phase="backoff", event_type="backoff_enter", ...)
                # Do NOT abandon; will be retried after backoff
            else:
                issue.retry_count += 1
                if issue.retry_count >= MAX_RETRIES:
                    # Abandon the issue
                    gh issue edit <issue.number> --remove-assignee @me --remove-label in-progress
                    log_conductor_event(phase="dispatch", event_type="human_escalate",
                                        payload={"issue_number": issue.number,
                                                 "reason": result.error_text,
                                                 "action_required": "manual investigation"})
                else:
                    gh issue edit <issue.number> --remove-assignee @me --remove-label in-progress
                    # issue returns to unassigned open state; will be re-selected next iteration

        elif result.subtype == "error_max_budget_usd":
            # Budget exhausted for this session; treat as rate-limit-style backoff
            governor.record_429()
            gh issue edit <issue.number> --remove-assignee @me --remove-label in-progress
            checkpoint.defer(issue.number)

        checkpoint.record_complete(issue.number)
        session.save(checkpoint, config)
        log_conductor_event(phase="dispatch", event_type="agent_completed", ...)

  REPEAT LOOP

COMPLETION GATE:
  # Called when batch is empty AND governor.active_count == 0

  STEP 5 — BLOCKING GAP ANALYSIS
    open_issues = gh_list_open_research_issues(milestone=milestone)
    for issue in open_issues:
        is_blocking = any(
            "impl" in tag for tag in issue.tags   # [BLOCKS_IMPL] tag present
        ) or _manually_classify(issue)
        if is_blocking:
            blocking_count += 1
        else:
            # Migrate to next research milestone
            next_research_milestone = find_next_research_milestone()
            gh issue edit <issue.number> --milestone <next_research_milestone>

    if blocking_count > 0:
        # Not done; go back to LOOP to dispatch remaining blockers
        continue LOOP

  STEP 6 — DECLARE RESEARCH COMPLETE
    # Zero blocking issues remain
    log_conductor_event(phase="complete", event_type="stage_complete", ...)

  STEP 7 — FILE NEXT PIPELINE ISSUE
    gh issue create \
        --title "Run design-worker for {milestone}" \
        --label "pipeline" \
        --milestone "{milestone}"

  STEP 8 — POST NOTION REPORT
    runner.run(
        prompt=f"Post session report to Notion...",
        allowed_tools=["mcp__notion__API-post-page"],
        ...
    )
    # Report title: "Research Session — {YYYY-MM-DD} — {repo}"
    # Notion parent page ID: 317bb275-6a02-803d-a59f-dc56c3527942

  STOP
```

### 4.3 Issue Sanitization

Before embedding any GitHub issue body in a prompt, apply:

```python
def sanitize(text: str, max_chars: int = 16_000) -> str:
    """Strip shell metacharacters and truncate."""
    # Remove characters that could break shell quoting: backtick, $(...), \
    text = re.sub(r'[`\\]', '', text)
    text = re.sub(r'\$\(', '(', text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[TRUNCATED — body exceeded 16,000 characters]"
    return text
```

---

## 5. Design Worker Loop

The design worker runs once per invocation. It does not dispatch sub-agents and does not loop. All work happens in the main process via direct `gh` CLI calls.

```
STEP 1 — READ ALL RESEARCH DOCS FOR THE MILESTONE

  closed_issues = gh issue list \
      --state closed \
      --label research \
      --milestone <research_milestone> \
      --json number,title,labels \
      --limit 200

  For each closed_issue:
      Find the corresponding file in docs/research/:
          doc_path = find_doc_for_issue(closed_issue.number)
          # e.g. docs/research/07-skill-adaptation.md for issue #7
      Read doc_path and extract:
          - Key findings (H2 sections, bullet points)
          - Deferred / V2_RESEARCH items (look for [V2_RESEARCH] tags)
          - Unresolved [INFERRED] claims that affect design
          - Cross-references to other docs

  Build a composite knowledge base:
      findings = {doc_slug: DocSummary, ...}
      deferred = [items tagged [V2_RESEARCH], ...]
      blocking_gaps = [items tagged [INFERRED] that affect current milestone design]

STEP 2 — IDENTIFY CORRESPONDING IMPL MILESTONE

  milestones = gh milestone list --repo <repo>
  impl_milestone = find_milestone_containing(milestones, "Impl", version=research_version)
  if impl_milestone is None:
      impl_milestone = gh api repos/<repo>/milestones \
          -f title="<version> Implementation" \
          -f description="Implementation phase for <version>"

STEP 3 — AUDIT EXISTING IMPL ISSUES (avoid duplication)

  existing = gh issue list \
      --state open \
      --milestone <impl_milestone> \
      --json number,title,labels,body \
      --limit 200
  existing_titles = {normalize(i.title) for i in existing}

STEP 4 — DESIGN AND FILE IMPLEMENTATION ISSUES

  For each logical unit of work derived from the research findings:
      a. DEFINE SCOPE
         Identify which module boundary (from CLAUDE.md) this unit touches.
         Each issue must be scoped to exactly one module.
         One issue per module per logical unit.

      b. WRITE ACCEPTANCE CRITERIA
         Concrete, testable, binary conditions for "done".
         No vague language ("should", "might") — only "must" and "does".

      c. WRITE TEST REQUIREMENTS
         What unit tests must exist. What integration tests (if any).
         Reference specific function or class names where possible.

      d. IDENTIFY DEPENDENCIES
         Which other new impl issues (by title/number) must complete first?
         Verify no circular dependencies before filing.

      e. ASSIGN LABEL
         Use the feat:* or infra label from CLAUDE.md module isolation table.

      f. SKIP IF DUPLICATE
         if normalize(proposed_title) in existing_titles: skip with log entry

      g. FILE ISSUE
         gh issue create \
             --repo <repo> \
             --title "<imperative verb phrase>" \
             --label "<feat:*>" \
             --milestone "<impl_milestone>" \
             --body "$(render_issue_body(context, criteria, scope, tests, deps, decisions))"

         filed_issues.append(new_issue_number)

STEP 5 — SET AND VERIFY DEPENDENCY ORDER

  Build a DAG from all filed issues' "Depends on" fields.
  Detect cycles: if any cycle exists, print error and fix by removing the least critical edge.
  Compute topological order.
  Print planned execution order:
      #N1 (no deps) -> #N2 (depends N1) -> #N3 (depends N1, N2) -> ...

STEP 6 — PRINT SUMMARY

  Print:
  - Implementation milestone name
  - Issues created: number, title, label, deps
  - Execution order (topological sort)
  - Research gaps that prevented full spec (if any) — flag as follow-up research issues
  - Verdict: "Ready for impl-worker — N issues filed, dependency graph complete."

STEP 7 — FILE NEXT PIPELINE ISSUE

  gh issue create \
      --title "Run impl-worker for <impl_milestone>" \
      --label "pipeline" \
      --milestone "<impl_milestone>"

STEP 8 — POST NOTION REPORT

  runner.run(
      prompt="Post design session report to Notion...",
      allowed_tools=["mcp__notion__API-post-page"],
      ...
  )
  # Report title: "Design Session — {YYYY-MM-DD} — {repo}"
  # Body: research milestone processed, impl issues filed, execution order, gaps

STOP
```

---

## 6. Impl Worker Loop

The impl worker loop claims implementation issues, dispatches sub-agent workers in parallel via isolated `claude -p` subprocesses, monitors CI, and squash-merges passing PRs. It enforces the usage governor before every dispatch.

### 6.1 Constants

```
CONCURRENCY_LIMIT = governor.max_concurrency    # tier-dependent; see Section 8
MAX_RETRIES = 2                                 # per-issue, per-agent failure
CI_POLL_INTERVAL = 30   # seconds between gh pr checks polls
REBASE_RETRY_LIMIT = 3  # how many times to attempt rebase before escalating
```

### 6.2 Main Loop Pseudocode

```
LOOP:

  STEP 1 — CHECK BACKOFF STATE
    if governor.backoff_until is not None and datetime.utcnow() < governor.backoff_until:
        log_conductor_event(phase="backoff", event_type="backoff_enter", ...)
        sleep until governor.backoff_until
        run probe:
            result = runner.run("OK", max_turns=1, allowed_tools=[], model=config.model, ...)
            if result.is_error: extend backoff; loop to STEP 1
        log_conductor_event(phase="backoff", event_type="backoff_exit", ...)

  STEP 2 — SELECT ISSUES TO DISPATCH
    n_slots = CONCURRENCY_LIMIT - governor.active_count
    if n_slots <= 0:
        wait for any active agent to complete; continue LOOP

    issues = gh issue list \
        --state open \
        --assignee "" \
        --milestone <milestone_or_all> \
        --json number,title,labels,body \
        --limit 200
    # Filter out: research-labeled issues, in-progress issues, pipeline issues.
    # Parse "Depends on: #N" from each issue body.
    # Remove issues whose dependencies include any open issue.
    unblocked = filter_unblocked(filter_non_research(issues))
    # Sort: highest downstream impact first, then bugs before features, then lowest number.
    ranked = sort_by_impact_then_type_then_number(unblocked)
    # Enforce module isolation: at most one active issue per module scope.
    batch = select_respecting_module_isolation(ranked, n_slots, governor.active_modules)

    if not batch:
        if governor.active_count == 0:
            go to COMPLETION
        else:
            wait for any active agent to complete; continue LOOP

  STEP 3 — CLAIM AND PREPARE BRANCHES (sequential)
    for issue in batch:
        a. branch_name = f"{issue.number}-{slugify(issue.title)}"
           # slugify: lowercase, replace spaces with hyphens, max 40 chars after number

        b. body = sanitize(issue.body)

        c. gh issue edit <issue.number> --add-assignee @me --add-label in-progress
           log_conductor_event(phase="claim", event_type="issue_claimed",
                               payload={"issue_number": issue.number, "branch": branch_name})

        d. git worktree add .claude/worktrees/<branch_name> -b <branch_name> origin/main
           git -C .claude/worktrees/<branch_name> push -u origin <branch_name>

        e. checkpoint.record_dispatch(issue.number, branch=branch_name,
                                      dispatch_time=datetime.utcnow().isoformat())
           session.save(checkpoint, config)

  STEP 4 — DISPATCH AGENTS IN PARALLEL
    For each issue in batch, build the sub-agent prompt:
        prompt = inject_skill("impl-worker",
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Issue: #{issue.number} — {issue.title}\n"
            f"- Branch: {branch_name}\n"
            f"- Allowed scope: {module_scope_for(issue)}\n\n"
            + body)

    results = asyncio.gather(*[
        runner.run(
            prompt=prompts[issue.number],
            allowed_tools=[
                "Bash(git *)", "Bash(gh *)",
                "Bash(uv *)", "Bash(python *)",
                "Read", "Edit", "Write", "Glob", "Grep",
            ],
            max_turns=config.max_turns,
            max_budget=config.max_budget if api_key_mode else None,
            model=config.model,
            env={
                **subprocess_env,
                "CLAUDE_CONFIG_DIR": f"/tmp/composer-agent-{issue.number}-{uuid4().hex}",
                # Sub-agents use isolated config dirs; no Notion MCP needed.
            },
            cwd=f".claude/worktrees/{branch_name}",
            log_context=LogContext(run_id=checkpoint.run_id, repo=repo,
                                   stage="implementation", issue_number=issue.number),
            data_dir=config.data_dir,
        )
        for issue in batch
    ])

  STEP 5 — MONITOR AND MERGE (as each agent completes)
    for (issue, result) in zip(batch, results):

        governor.record_result(result)

        if result.is_error:
            error_class = classify_error(result)

            if error_class == "rate_limit":
                governor.record_429()
                gh issue edit <issue.number> --remove-assignee @me --remove-label in-progress
                checkpoint.defer(issue.number)
                git worktree remove --force .claude/worktrees/<branch_name>
                git push origin --delete <branch_name>
                continue  # skip CI/merge; issue will be requeued after backoff

            elif error_class == "implementation_failure":
                issue.retry_count += 1
                git worktree remove --force .claude/worktrees/<branch_name>
                if issue.retry_count < MAX_RETRIES:
                    gh issue edit <issue.number> --remove-assignee @me --remove-label in-progress
                    git push origin --delete <branch_name>
                    # Issue returns to open state; re-selected in a future iteration
                else:
                    # Exhausted retries — escalate
                    log_conductor_event(phase="dispatch", event_type="human_escalate",
                                        payload={"issue_number": issue.number,
                                                 "reason": "exceeded MAX_RETRIES",
                                                 "action_required": "manual investigation"})
                    gh issue edit <issue.number> --remove-assignee @me --remove-label in-progress
                continue

        # SUCCESS path: agent created a PR
        pr_number = find_pr_for_branch(branch_name)
        if pr_number is None:
            # Agent completed but created no PR — treat as failure
            log and abandon the issue; continue

        log_conductor_event(phase="dispatch", event_type="pr_created",
                            payload={"issue_number": issue.number, "pr_number": pr_number})

        # CI polling loop
        ci_status = "pending"
        rebase_attempts = 0
        while ci_status != "pass":
            sleep(CI_POLL_INTERVAL)
            ci_status = gh_pr_checks_status(pr_number)
            log_conductor_event(phase="ci_check", event_type="ci_checked",
                                payload={"pr_number": pr_number, "status": ci_status})

            if ci_status == "fail":
                failure_type = classify_ci_failure(pr_number)

                if failure_type == "conflict":
                    if rebase_attempts >= REBASE_RETRY_LIMIT:
                        log_conductor_event(..., event_type="human_escalate", ...)
                        break  # stop retrying; leave PR open for human
                    git -C .claude/worktrees/<branch_name> fetch origin
                    git -C .claude/worktrees/<branch_name> rebase origin/main
                    # On conflict in files outside agent scope:
                    #   log human_escalate; abort rebase; break
                    # On conflict in files in scope: resolve automatically
                    git -C .claude/worktrees/<branch_name> push --force-with-lease origin <branch_name>
                    rebase_attempts += 1
                    ci_status = "pending"  # reset; re-poll

                elif failure_type == "flaky":
                    # Re-trigger CI by pushing an empty commit
                    git -C .claude/worktrees/<branch_name> commit --allow-empty \
                        -m "ci: re-trigger after flaky failure"
                    git -C .claude/worktrees/<branch_name> push origin <branch_name>
                    ci_status = "pending"

                elif failure_type == "scope_violation":
                    # Agent edited files outside its allowed scope
                    log_conductor_event(..., event_type="human_escalate",
                                        payload={"reason": "scope violation", ...})
                    break  # leave for human

                else:
                    # Unknown CI failure; escalate after MAX_RETRIES
                    issue.ci_fail_count += 1
                    if issue.ci_fail_count >= MAX_RETRIES:
                        log_conductor_event(..., event_type="human_escalate", ...)
                        break
                    ci_status = "pending"

        if ci_status == "pass":
            # Check reviews
            reviews = gh pr view <pr_number> --json reviews
            review_status = aggregate_review_status(reviews)
            # review_status: "approved" | "changes_requested" | "no_review"
            # "no_review" is treated as approved (no mandatory review gate)

            if review_status == "changes_requested":
                # Triage the feedback
                comments = gh api repos/<repo>/pulls/<pr_number>/comments
                for comment in comments:
                    if is_in_scope(comment) and is_straightforward(comment):
                        apply_fix_via_runner(comment, branch_name, issue)
                    elif is_valid_out_of_scope(comment):
                        gh issue create --title "Follow-up: <comment summary>" ...
                        gh pr comment <pr_number> --body "Filed #<new_issue> for this"
                    else:
                        gh pr comment <pr_number> --body "Skipping: <reason>"

            # Squash merge
            gh pr merge <pr_number> --squash --delete-branch
            git pull origin main
            git worktree remove --force .claude/worktrees/<branch_name>

            log_conductor_event(phase="merge", event_type="pr_merged",
                                payload={"pr_number": pr_number, "issue_number": issue.number})
            checkpoint.record_complete(issue.number)
            session.save(checkpoint, config)
            log_conductor_event(phase="merge", event_type="checkpoint_write", ...)

        # Re-survey: check for newly unblocked issues
        # Immediately dispatch into freed module slot if work is available

  REPEAT LOOP

COMPLETION:
  # All issues processed; no active agents; no dispatchable work

  STEP 6 — PRINT TERMINAL SUMMARY
    - Issues attempted
    - PRs merged (with PR numbers)
    - Issues failed/abandoned (with reasons)
    - Newly unblocked issues available for next run (from remaining open issues)

  STEP 7 — FILE NEXT PIPELINE ISSUE
    gh issue create \
        --title "Run plan-milestones for <next_version>" \
        --label "pipeline"
    # next_version is inferred from the current milestone's version identifier

  STEP 8 — POST NOTION REPORT
    runner.run(
        prompt="Post session report to Notion...",
        allowed_tools=["mcp__notion__API-post-page"],
        mcp_config=NOTION_MCP_CONFIG_PATH,
        env=subprocess_env,  # no CLAUDE_CONFIG_DIR — orchestrator uses user config
        ...
    )
    # Report title: "Session Report — {YYYY-MM-DD} — {repo}"
    # Notion parent page ID: 317bb275-6a02-803d-a59f-dc56c3527942

  STOP
```

---

## 7. Skill Injection Model

### 7.1 Overview

Skills are plain markdown files stored in `src/composer/skills/`. They contain the step-by-step instructions that a headless Claude Code subprocess follows when running as a research-worker, impl-worker, or design-worker. They are not slash-command skill packages — they cannot be invoked with `/skill-name` in interactive mode. Instead, `cli.py` reads them at dispatch time, renders runtime values into them, and passes the rendered string as the `-p` prompt.

This is the "Path A — Prompt injection" model from `docs/research/07-skill-adaptation.md §1.2`.

### 7.2 `inject_skill` Function

```python
def inject_skill(skill_name: str, base_prompt: str) -> str:
    """
    Read skills/<skill_name>.md, apply the headless auto-resolve policy substitution,
    prepend a session-parameters header, and return the composed prompt.

    Args:
        skill_name: Filename stem without extension (e.g. "research-worker").
        base_prompt: Session-specific context to prepend before the skill body.
                     Must already have runtime values substituted (repo, milestone, etc.).

    Returns:
        Full prompt string ready to pass to runner.run() as the -p argument.

    Raises:
        FileNotFoundError: if skills/<skill_name>.md does not exist.
    """
    skill_path = Path(__file__).parent / "skills" / f"{skill_name}.md"
    skill_body = skill_path.read_text(encoding="utf-8")
    skill_body = _apply_headless_policy(skill_body)
    return base_prompt + "\n\n---\n\n" + skill_body
```

The `base_prompt` is assembled by the caller in `cli.py` and contains:
- The `## Session Parameters` block (repo, milestone, issue number, date)
- The sanitized GitHub issue body (for per-issue dispatches)

### 7.3 Auto-Resolve Policy (Headless Gate Replacement)

The skill files contain a startup check that in interactive mode says "ask the user before cleaning up orphaned work." In headless `-p` mode there is no user to ask. `inject_skill` applies a string substitution at read time to replace the interactive gate with a deterministic auto-resolve policy.

**Pattern replaced (present in `impl-worker.md` and `research-worker.md`):**
```
ask the user before cleaning up — this is the only confirmation gate.
```

**Replacement (headless auto-resolve policy):**
```
apply the headless auto-resolve policy — no user available in -p mode:
  * Stale worktree + no open PR -> git worktree remove --force <path>
  * In-progress issue + open PR -> leave alone (active work; orchestrator handles)
  * In-progress issue + no PR + branch has commits -> remove in-progress label,
    preserve branch for human review, log orphan_auto_resolved event
  * In-progress issue + no PR + branch has no commits -> remove in-progress label,
    delete branch, log orphan_auto_resolved event
All auto-resolve decisions are logged with event_type="orphan_auto_resolved".
```

This substitution is implemented as `_apply_headless_policy(skill_body: str) -> str` inside `cli.py`. See `docs/research/07-skill-adaptation.md §2.1` and `§3.2` for the full rationale and gate disposition table.

### 7.4 Notion Reporting in Skills

Skill files include a "Post Notion report" step that calls `mcp__notion__API-post-page`. This step runs inside the orchestrator's own `claude -p` session (not inside a sub-agent). The orchestrator session:

- Does NOT use `CLAUDE_CONFIG_DIR` isolation (required to access user MCP config)
- Uses `@notionhq/notion-mcp-server` (local stdio) with a static integration token
- Reads the token from `NOTION_TOKEN` env var, injected via `.mcp.json` at the repo root

The Notion MCP config path is resolved from `CONDUCTOR_NOTION_MCP_CONFIG` env var, falling back to `{repo_root}/.mcp.json` if the env var is not set. The config path is passed to `runner.run()` via the `mcp_config` parameter.

**Why not the hosted Notion MCP (`mcp.notion.com`):** OAuth tokens expire in approximately one hour and Claude Code does not auto-refresh them in `-p` mode. The local stdio server with a static integration token does not expire. See `docs/research/28-notion-mcp-oauth.md §4`.

---

## 8. Usage Governor

`UsageGovernor` is a class defined in `cli.py` and instantiated once per worker invocation. It sits between the issue queue and the dispatch loop, enforcing concurrency limits and managing rate-limit backoff.

### 8.1 Class Definition

```python
class UsageGovernor:
    """
    Enforces concurrency limits and manages rate-limit backoff for agent dispatch.

    Concurrency limits by subscription tier (CONDUCTOR_SUBSCRIPTION_TIER env var):
        Pro     -> max_concurrency = 2
        Max5x   -> max_concurrency = 3
        Max20x  -> max_concurrency = 5
        api_key -> max_concurrency = 3  (default; governed by API tier RPM limits)
    """

    def __init__(self, config: Config) -> None:
        self.max_concurrency: int = _resolve_concurrency(
            os.environ.get("CONDUCTOR_SUBSCRIPTION_TIER", "Max5x")
        )
        self.active_count: int = 0
        self.active_modules: set[str] = set()
        self.backoff_until: datetime | None = None
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._backoff_attempt: int = 0
```

### 8.2 `can_dispatch(n_agents: int) -> bool`

Returns `True` if it is safe to dispatch `n_agents` new agents right now.

```python
def can_dispatch(self, n_agents: int = 1) -> bool:
    """
    Checks three gates in order:

    1. Backoff gate: if backoff_until is in the future, return False.
    2. Concurrency gate: if active_count + n_agents > max_concurrency, return False.
    3. Budget gate: (future) estimated_budget_remaining > estimated_cost.
                    Not implemented in MVP; always passes.
    """
    # Gate 1: backoff
    if self.backoff_until is not None:
        if datetime.utcnow() < self.backoff_until:
            return False
        else:
            self.backoff_until = None     # backoff expired
            self._backoff_attempt = 0

    # Gate 2: concurrency
    if self.active_count + n_agents > self.max_concurrency:
        return False

    # Gate 3: budget (MVP: no-op)
    return True
```

### 8.3 `record_429() -> None`

Called when a sub-agent result is classified as a rate-limit error. Sets an exponential backoff.

```python
def record_429(self) -> None:
    """
    Sets backoff_until using an exponential formula:

        delay = min(3600, 30 * 2**attempt) + random(0, 0.25 * base)

    Where:
        base_delay = 30 seconds
        max_delay  = 3600 seconds (1 hour)
        jitter     = uniform(0, base_delay * 0.25) = uniform(0, 7.5 seconds)

    For confirmed full-window exhaustion (the reset time is parseable from the
    error message), backoff_until is set to the parsed reset timestamp instead.
    """
    base = 30
    max_delay = 3600
    delay = min(max_delay, base * (2 ** self._backoff_attempt))
    jitter = random.uniform(0, base * 0.25)
    self.backoff_until = datetime.utcnow() + timedelta(seconds=delay + jitter)
    self._backoff_attempt += 1
```

### 8.4 `record_result(result_event: dict) -> None`

Called after each sub-agent completes. Updates running token totals for budget tracking.

```python
def record_result(self, result_event: dict) -> None:
    """
    Updates cumulative token counts from the result event.
    Used for conservative budget accounting; does not make dispatch decisions
    directly (that is can_dispatch's job in gate 3).
    """
    usage = result_event.get("usage", {})
    self._total_input_tokens += usage.get("input_tokens", 0)
    self._total_output_tokens += usage.get("output_tokens", 0)
    self.active_count = max(0, self.active_count - 1)
```

### 8.5 Concurrency Limit Table

| Tier (`CONDUCTOR_SUBSCRIPTION_TIER`) | `max_concurrency` | Rationale |
|--------------------------------------|-------------------|-----------|
| `Pro` | 2 | 5 parallel agents exhaust Pro in ~15 min; 1-2 prevents runaway |
| `Max5x` | 3 | 5 agents exhaust Max 5x in ~75 min; 2-3 gives reasonable session lifetime |
| `Max20x` | 5 | 20x budget; rarely exhausted in practice |
| `api_key` | 3 | Default; user configures RPM limits separately |

Source: `docs/research/08-usage-scheduling.md §4`.

---

## 9. `composer` Admin Commands

The `composer` Click group exposes two subcommands for human operators. Neither command dispatches agents or enters a loop.

### 9.1 `composer health`

```python
@composer.command("health")
@click.option("--repo", default=None, help="Repo to check (OWNER/REPO)")
def health(repo: str | None) -> None:
    """Run preflight checks and print results."""
```

**Execution:**

```
1. config = Config()
2. report = health.check_all(repo=repo, config=config)
3. For each item in report.items:
       print(item.format())   # "OK", "WARN", or "FATAL" prefix + description
4. if report.has_fatal:
       sys.exit(1)
   else:
       sys.exit(0)
```

**Checks performed by `health.check_all()` (defined in `health.py`):**

| Check | Fatal? | Description |
|-------|--------|-------------|
| `claude` binary exists | Yes | `which claude` must succeed |
| `gh` binary exists | Yes | `which gh` must succeed |
| `git` binary exists | Yes | `which git` must succeed |
| `gh auth status` | Yes | User must be authenticated with `gh` |
| Repo accessible | Yes (if `--repo` given) | `gh repo view <repo>` must succeed |
| Auth mode detectable | No (warn) | `ANTHROPIC_API_KEY` present or subscription auth detectable |
| `NOTION_TOKEN` set | No (warn) | Required for Notion reporting; missing is non-fatal |

### 9.2 `composer cost`

```python
@composer.command("cost")
@click.option("--repo", default=None, help="Filter to a specific repo (OWNER/REPO)")
@click.option("--stage", default=None, help="Filter to a specific stage")
def cost(repo: str | None, stage: str | None) -> None:
    """Show cost ledger summary."""
```

**Execution:**

```
1. config = Config()
2. entries = logger.read_cost_ledger(config.data_dir)
3. if repo: entries = [e for e in entries if e["repo"] == repo]
4. if stage: entries = [e for e in entries if e["stage"] == stage]
5. aggregated = aggregate_by_repo_and_stage(entries)
   # Group by repo -> stage -> sum(input_tokens, output_tokens, cache_*, total_cost_usd)
   #                         count(sessions, errors)
6. Print table(s) per repo (see docs/design/lld/logger.md §7.2 for format)
7. Print grand total line
8. sys.exit(0)
```

The output format is defined in `docs/design/lld/logger.md §7.2`. The column header reads `Est. Cost (USD)` because subscription sessions report estimated (not billed) cost.

---

## 10. Interface Summary

### 10.1 What `cli.py` Exports

| Symbol | Kind | Signature | Consumers |
|--------|------|-----------|-----------|
| `impl_worker` | Click command | Entry point; see Section 2 | `pyproject.toml` entry point |
| `research_worker` | Click command | Entry point; see Section 2 | `pyproject.toml` entry point |
| `design_worker` | Click command | Entry point; see Section 2 | `pyproject.toml` entry point |
| `composer` | Click group | Parent for `health` and `cost` | `pyproject.toml` entry point |
| `inject_skill` | function | `(skill_name: str, base_prompt: str) -> str` | Internal; worker functions |
| `UsageGovernor` | class | `(config: Config)` | Internal; worker functions |

### 10.2 What `skills/` Provides

| Skill file | Purpose | Called by |
|-----------|---------|-----------|
| `research-worker.md` | Headless orchestrator instructions for running a research agent dispatch loop | `research_worker()` via `inject_skill("research-worker", ...)` |
| `impl-worker.md` | Headless orchestrator instructions for implementation issue dispatch, CI monitoring, and PR merge | `impl_worker()` via `inject_skill("impl-worker", ...)` |
| `design-worker.md` | Instructions for translating research docs into scoped impl issues; no sub-agents | `design_worker()` via `inject_skill("design-worker", ...)` |
| `plan-milestones.md` | Instructions for milestone planning and seed research issue filing; intended for interactive use or a future `plan-milestones` entry point | Not yet wired to a CLI entry point |

### 10.3 What `cli.py` Does NOT Do

- Does not implement `runner.run()` — that is `runner.py`'s responsibility.
- Does not define log schemas — that is `logger.py`'s responsibility.
- Does not define `Config` fields — that is `config.py`'s responsibility.
- Does not perform preflight checks directly — delegates to `health.check_all()`.
- Does not read or write the cost ledger directly — calls `logger.read_cost_ledger()`.
- Does not parse `stream-json` output — that is `runner.py`'s responsibility.

---

## 11. Cross-References

- **`docs/research/07-skill-adaptation.md`**: Establishes the "Path A — Prompt injection" model for skills in `-p` mode; documents the auto-resolve policy for orphaned work; defines `--allowedTools` lists per worker type; documents the `--mcp-config` / `--append-system-prompt-file` layering pattern.
- **`docs/research/08-usage-scheduling.md`**: Defines concurrency limits per subscription tier; documents the exponential backoff formula; documents the `UsageGovernor` design; establishes that `--max-budget-usd` is API-key-only.
- **`docs/research/28-notion-mcp-oauth.md`**: Documents why the hosted Notion MCP is not viable for headless use (OAuth token expiry in `-p` mode); establishes that `@notionhq/notion-mcp-server` with `NOTION_TOKEN` is the correct pattern.
- **`docs/research/01-agent-tool-in-p-mode.md`**: Establishes that the subprocess-based worker dispatch pattern (via `Bash` tool calling `claude -p`) is preferred over the in-process `Agent` tool; documents the 50K token overhead and the 4-layer isolation mitigation; confirms `isolation: worktree` works in headless mode.
- **`src/composer/runner.py`**: Implements `runner.run()` — the function that `cli.py` calls to launch each `claude -p` subprocess, parse stream-json output, and return a result event.
- **`src/composer/session.py`**: Implements `session.load()`, `session.new()`, `session.save()` — checkpoint persistence used by the worker startup sequence and loop.
- **`src/composer/logger.py`**: Implements `log_conductor_event()`, `log_cost()`, `read_cost_ledger()` — called by `cli.py` throughout the worker loop.
- **`src/composer/health.py`**: Implements `health.check_all()` — called during startup sequence.
- **`src/composer/config.py`**: Defines `Config` — instantiated at startup; fields read by all worker functions.
- **`docs/design/lld/logger.md`**: Defines conductor log event schemas and cost table output format referenced in Sections 4, 5, 6, and 9.
