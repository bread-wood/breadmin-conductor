# Research: CLAUDE.md Opt-Out and Hash-Pinning in Headless -p Mode

**Issue:** #18
**Milestone:** M1: Foundation
**Status:** Research Complete
**Date:** 2026-03-02
**Spawned From:** Issue #6 (Security threat model, section T2 and follow-up R-SEC-B)

---

## Table of Contents

1. [Summary of Findings](#summary-of-findings)
2. [CLAUDE.md Loading Sequence in -p Mode](#claudemd-loading-sequence-in--p-mode)
3. [Opt-Out Mechanisms](#opt-out-mechanisms)
   - [CLAUDE_CODE_SIMPLE: Full Disable](#31-claude_code_simple-full-disable)
   - [claudeMdExcludes: Selective Exclusion](#32-claudemdexcludes-selective-exclusion)
   - [--setting-sources + CLAUDE_CONFIG_DIR: Structural Isolation](#33---setting-sources--claude_config_dir-structural-isolation)
   - [No --no-project-md Flag](#34-no---no-project-md-flag)
4. [Worktree CLAUDE.md Loading Behavior](#worktree-claudemd-loading-behavior)
5. [Hash-Pinning: Design and Feasibility](#hash-pinning-design-and-feasibility)
6. [Pre-Dispatch CLAUDE.md Review Checklist](#pre-dispatch-claudemd-review-checklist)
7. [Layered Defense Architecture](#layered-defense-architecture)
8. [Cross-References](#cross-references)
9. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
10. [Sources](#sources)

---

## Summary of Findings

The core threat (documented in `06-security-threat-model.md` T2) is that `claude -p` automatically loads `.claude/CLAUDE.md` from the working directory without trust verification, and a maliciously crafted CLAUDE.md can hijack agent behavior.

**Key findings in brief:**

| Question | Answer | Confidence |
|----------|--------|------------|
| Is there a `--no-project-md` flag? | No. Does not exist. | [DOCUMENTED] |
| Can CLAUDE.md loading be fully disabled? | Yes, via `CLAUDE_CODE_SIMPLE=1` | [DOCUMENTED] |
| Does `CLAUDE_CODE_SIMPLE=1` preserve enough tools for sub-agents? | No — it strips hooks, MCP tools, and attachments; too limiting for conductor agents | [DOCUMENTED] |
| Can specific CLAUDE.md files be excluded? | Yes, via `claudeMdExcludes` | [DOCUMENTED] |
| Does `claudeMdExcludes` work in -p mode? | Yes — it is a settings-layer mechanism, not interactive-only | [INFERRED] |
| Can `--setting-sources ""` suppress project CLAUDE.md loading? | No — `--setting-sources` controls *settings files*, not CLAUDE.md loading | [DOCUMENTED] |
| Is there a signing or provenance check for CLAUDE.md? | No. Anthropic provides no hash verification, signing, or provenance mechanism. | [DOCUMENTED] |
| Is conductor-side hash-pinning feasible? | Yes — SHA256 pre-dispatch check is implementable in Python with no external deps | [INFERRED] |
| Do worktrees load the parent repo's CLAUDE.md? | Yes, due to upward traversal across the `.git` file boundary — open bug as of March 2026 | [DOCUMENTED] |
| Can `SessionStart` hooks verify CLAUDE.md before agent acts? | Yes, but hooks are not available if CLAUDE.md is already loaded before `SessionStart` fires | [INFERRED] |

---

## CLAUDE.md Loading Sequence in -p Mode

### 2.1 Trust Verification is Silently Disabled

The official security documentation states:

> **Trust verification**: First-time codebase runs and new MCP servers require trust verification.
> Note: Trust verification is disabled when running non-interactively with the `-p` flag.

This means: in headless `-p` mode, there is no "do you trust the files in this folder?" prompt. CLAUDE.md is loaded immediately, unconditionally, as if the directory were fully trusted. This was flagged as a documentation gap in GitHub issue #20253 (opened 2025, closed as "not planned" February 28, 2026 due to inactivity — the underlying behavior was never changed).

### 2.2 Load Order

From the official memory documentation, CLAUDE.md files are resolved by **walking up the directory tree from CWD** to the filesystem root. For a sub-agent with CWD `/repos/myrepo/.claude/worktrees/7-my-feature/`:

| Scope | Location | Load order |
|-------|----------|------------|
| Managed policy | `/Library/Application Support/ClaudeCode/CLAUDE.md` (macOS) | First, always |
| User | `~/.claude/CLAUDE.md` | Second |
| Ancestor dirs | Walk up from CWD to `/` — loads every CLAUDE.md and CLAUDE.local.md found | Third |
| Project | `{cwd}/CLAUDE.md` or `{cwd}/.claude/CLAUDE.md` | Loaded as part of walk |
| Local | `{cwd}/CLAUDE.local.md` | Loaded during walk |

Files in subdirectories *below* CWD are loaded **on demand** when Claude reads files in those directories, not at startup.

**The managed policy CLAUDE.md cannot be excluded by any user or project setting.** This is the one file that is always loaded regardless of `claudeMdExcludes` configuration.

### 2.3 CLAUDE.md as Context, Not Enforcement

An important nuance: CLAUDE.md content is loaded into the session as context, not as enforced configuration. The documentation states: "Claude treats them as context, not enforced configuration." This means a sufficiently injected CLAUDE.md can only influence model behavior to the degree the model follows it — strong identity anchoring in the conductor's own prompts can partially resist malicious CLAUDE.md instructions. However, this is not a reliable defense: adversarial CLAUDE.md content can claim authority, override scope, and instruct the agent to take actions outside its assigned task.

### 2.4 SDK vs. CLI Distinction

**This is critical for conductor architecture:**

- **CLI (`claude -p`)**: Auto-discovers and loads CLAUDE.md based on CWD hierarchy. No opt-in required; no opt-out available via standard flags.
- **Python Agent SDK**: CLAUDE.md is **only** loaded if `setting_sources=["project"]` is explicitly set. The `claude_code` system prompt preset alone does NOT auto-load CLAUDE.md.

This SDK distinction is architecturally significant: if conductor migrates from subprocess-based spawning (`claude -p`) to the Python Agent SDK, it gains fine-grained control over whether target repo CLAUDE.md files are loaded at all. See section 9.1 (follow-up research recommendation).

---

## Opt-Out Mechanisms

### 3.1 `CLAUDE_CODE_SIMPLE=1`: Full Disable

**Confidence: [DOCUMENTED]**

Setting `CLAUDE_CODE_SIMPLE=1` in the subprocess environment disables CLAUDE.md loading entirely. From the official settings documentation:

> Set to `1` to run with a minimal system prompt and only the Bash, file read, and file edit tools. Disables MCP tools, attachments, hooks, and CLAUDE.md files.

**What it preserves:** Bash, file read (Read), file write/edit (Edit/Write) tools only.

**What it removes:**
- All CLAUDE.md files (at all scopes — project, user, ancestor)
- MCP tools
- Hooks (PreToolUse, PostToolUse, etc.)
- Attachments

**Assessment for conductor use:** This setting is too destructive for conductor sub-agents. Conductor's defense model (from `06-security-threat-model.md`) depends on PreToolUse and PostToolUse hooks for runtime command validation and audit logging. `CLAUDE_CODE_SIMPLE=1` strips those hooks, leaving the agent with no behavioral guardrails beyond `--allowedTools`/`--disallowedTools`. Additionally, this mode removes MCP tools, which issue-worker agents need for GitHub operations.

**Assessment for research-worker agents:** Potentially viable in a stripped-down configuration — research workers only need Read, Edit, WebFetch, and Bash tools for git operations. However, loss of hooks means the PostToolUse JSONL audit log cannot be implemented, and WebFetch may not be available in SIMPLE mode (the documentation does not clarify whether WebFetch survives). This needs empirical verification.

**Recommendation:** Do not use `CLAUDE_CODE_SIMPLE=1` as the primary CLAUDE.md defense for conductor agents. Use it only for the narrowest possible ad-hoc tasks where hooks and MCP are genuinely not needed.

### 3.2 `claudeMdExcludes`: Selective Exclusion

**Confidence: [DOCUMENTED]**

The `claudeMdExcludes` setting in any settings layer lets you skip specific CLAUDE.md files by path or glob pattern. From the official memory documentation:

> Patterns are matched against **absolute file paths** using glob syntax. You can configure `claudeMdExcludes` at any settings layer: user, project, local, or managed policy. Arrays merge across layers.

**Exact JSON syntax** (in `.claude/settings.local.json`):

```json
{
  "claudeMdExcludes": [
    "**/monorepo/CLAUDE.md",
    "/home/user/monorepo/other-team/.claude/rules/**",
    "/absolute/path/to/target-repo/.claude/CLAUDE.md"
  ]
}
```

Patterns use glob syntax and are matched against the **absolute** file path of each discovered CLAUDE.md. A pattern like `/absolute/path/to/target-repo/.claude/CLAUDE.md` would suppress exactly that file.

**Critical limitation:** Managed policy CLAUDE.md files (at `/Library/Application Support/ClaudeCode/CLAUDE.md` on macOS) **cannot be excluded**. This ensures organization-wide instructions always apply. For conductor's use case on a developer machine without a managed policy file deployed, this is not a practical limitation.

**How to use for conductor sub-agents:**

The conductor cannot write to the target repo's `.claude/settings.local.json` to pre-deploy exclusions (that would modify the target repo, which is outside conductor's scope). However, conductor can:

1. Write `claudeMdExcludes` to the **user settings** at `~/.claude/settings.json` — but this would affect the entire machine globally.
2. Write `claudeMdExcludes` to a **temporary settings file** passed via `--settings` — this takes second-highest precedence and would add the exclusion for that invocation only.
3. Write `claudeMdExcludes` to `.claude/settings.local.json` **within the worktree** — this is the cleanest approach since worktrees are ephemeral and the file is gitignored.

**Recommended pattern for conductor:**

Before spawning each sub-agent, write a `settings.local.json` into the worktree with the target repo's CLAUDE.md excluded:

```python
import json, os

def write_worktree_settings(worktree_path: str, target_repo_claude_md: str) -> None:
    """Write settings.local.json into the worktree to exclude the target repo's CLAUDE.md."""
    settings = {
        "claudeMdExcludes": [
            target_repo_claude_md,  # absolute path to the target repo's CLAUDE.md
        ]
    }
    settings_path = os.path.join(worktree_path, ".claude", "settings.local.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
```

This technique was documented in `04-configuration.md` section 1.2 and referenced as a tool for excluding monorepo sibling CLAUDE.md files. It applies equally to excluding adversarial target-repo CLAUDE.md files.

**Note:** `claudeMdExcludes` only excludes the *file* from loading. The conductor's own CLAUDE.md (at `~/.claude/CLAUDE.md` and the conductor repo's `.claude/CLAUDE.md`) will still load normally. This is the desired behavior: conductor orchestration rules apply, target repo CLAUDE.md does not.

### 3.3 `--setting-sources` + `CLAUDE_CONFIG_DIR`: Structural Isolation

**Confidence: [DOCUMENTED] for settings isolation; [INFERRED] for CLAUDE.md impact**

From `10-settings-mcp-injection.md`, the `--setting-sources ""` flag suppresses loading of user and project *settings files*. The combination:

```bash
CLAUDE_CONFIG_DIR=/tmp/isolated-config \
  claude -p \
  --setting-sources "" \
  --settings ./generated-agent-settings.json \
  --dangerously-skip-permissions \
  "$PROMPT"
```

...eliminates all user settings, project settings, and user MCP server configs. However:

**`--setting-sources` does NOT affect CLAUDE.md loading.** CLAUDE.md loading is controlled by the memory system, not the settings system. Even with `--setting-sources ""`, the memory file walk-up from CWD still occurs, and CLAUDE.md files are still loaded.

`CLAUDE_CONFIG_DIR` similarly does not suppress CLAUDE.md loading. It isolates Claude Code's session state, history, and `~/.claude.json` MCP configs, but not the memory file walk-up.

**Conclusion:** Neither `--setting-sources` nor `CLAUDE_CONFIG_DIR` alone provides CLAUDE.md isolation. Use `claudeMdExcludes` (section 3.2) or `CLAUDE_CODE_SIMPLE` (section 3.1) for CLAUDE.md suppression. Use `--setting-sources + CLAUDE_CONFIG_DIR` as a complementary settings isolation layer.

### 3.4 No `--no-project-md` Flag

**Confidence: [DOCUMENTED]**

As of March 2026, there is no `--no-project-md`, `--no-claude-md`, `--skip-memory-files`, `--disable-project-md`, or equivalent CLI flag. The issue tracker confirms no such flag has been added or is currently planned. Feature requests for more granular CLAUDE.md control exist but are open without roadmap commitment:

- Issue #20880: Exclude parent CLAUDE.md files from auto-loading (open, no milestone)
- Issue #16600: Memory traversal should respect git worktree boundaries (open, no milestone)

The closest built-in mechanism is `CLAUDE_CODE_SIMPLE=1`, but as noted above, it is too broad for conductor use.

---

## Worktree CLAUDE.md Loading Behavior

### 4.1 The Upward Traversal Problem

**Confidence: [DOCUMENTED]**

When a sub-agent runs in `.claude/worktrees/7-my-feature/`, its CWD is inside the conductor's git repo. CLAUDE.md traversal walks upward from this CWD through the filesystem. A git worktree has a `.git` *file* (not directory) at its root pointing to the main repo's `.git/worktrees/<name>/` directory. Claude Code does **not** stop traversal at this `.git` file boundary.

As a result, the sub-agent in a worktree at `/repos/conductor/.claude/worktrees/7-my-feature/` will load CLAUDE.md files from:
1. `/repos/conductor/.claude/worktrees/7-my-feature/CLAUDE.md` (if present)
2. `/repos/conductor/.claude/CLAUDE.md` (the conductor's own instructions)
3. `/repos/conductor/CLAUDE.md` (the conductor's root-level CLAUDE.md)
4. `/repos/CLAUDE.md` (if present)
5. Continue up to filesystem root...

This has two implications:
- **Desired behavior:** The conductor's own `.claude/CLAUDE.md` is loaded, giving the sub-agent orchestration rules. This is intentional.
- **Undesired behavior:** If the worktree is for a *different* target repo (not a worktree of the conductor itself), walking up from that worktree will eventually reach that target repo's CLAUDE.md. Adversarial content in that file will be loaded.

GitHub issue #16600 ("Claude Code memory traversal should respect git worktree boundaries") documents this as an open bug with 2x token consumption and conflicting instructions as observed symptoms. As of March 1, 2026, it remains open with no resolution or roadmap milestone.

### 4.2 Mitigation: claudeMdExcludes in Worktree Settings

The recommended mitigation for the upward traversal problem is to write `claudeMdExcludes` entries into `.claude/settings.local.json` within the worktree, specifically targeting the absolute path(s) of the target repo's CLAUDE.md files. See section 3.2 for the implementation pattern.

For conductor's specific case (conductor owns the worktrees, not a foreign repo), the sub-agent in `.claude/worktrees/7-my-feature/` is already in the conductor repo. The loaded CLAUDE.md files are the conductor's own, which is the desired behavior. No exclusion is needed for same-repo worktrees.

The exclusion becomes necessary when conductor spawns an agent whose CWD is in a *different* repo entirely (multi-repo conductor scenario — see issue #39 on multi-repo orchestration). In that case, the target repo's CLAUDE.md will be loaded and must be excluded via the pattern in section 3.2.

---

## Hash-Pinning: Design and Feasibility

### 5.1 Anthropic Provides No Built-In Hash Verification

**Confidence: [DOCUMENTED]**

Claude Code provides no signing mechanism, provenance check, or content hash verification for CLAUDE.md files. The trust model for CLAUDE.md is entirely implicit: the file is trusted because it is present in the working directory. There is no equivalent of `npm integrity` or `pip hash` verification for memory files.

### 5.2 Conductor-Side Hash-Pinning: Design

Hash-pinning must be implemented entirely in the conductor as a **pre-dispatch check**. The design:

1. **First run (registration):** When a new repo is added to conductor's scope, compute the SHA256 of its CLAUDE.md and store it in conductor's own config.
2. **Every dispatch:** Before spawning a sub-agent, recompute the SHA256 and compare. Abort if mismatch.
3. **On legitimate change:** Operator re-registers the new hash after reviewing the change.

**Implementation (Python):**

```python
import hashlib, os, json
from pathlib import Path

def compute_claude_md_hash(repo_path: str) -> str | None:
    """
    Compute SHA256 of the target repo's CLAUDE.md.
    Returns None if the file does not exist.
    Checks both .claude/CLAUDE.md and CLAUDE.md at repo root.
    """
    for candidate in [
        os.path.join(repo_path, ".claude", "CLAUDE.md"),
        os.path.join(repo_path, "CLAUDE.md"),
    ]:
        if os.path.isfile(candidate):
            with open(candidate, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
    return None  # no CLAUDE.md present

def register_claude_md_hash(repo_path: str, hashes_file: Path) -> None:
    """Register (or update) the known-good hash for a repo's CLAUDE.md."""
    h = compute_claude_md_hash(repo_path)
    hashes = json.loads(hashes_file.read_text()) if hashes_file.exists() else {}
    hashes[repo_path] = h  # None means "file absent, that is expected"
    hashes_file.write_text(json.dumps(hashes, indent=2))

def verify_claude_md_hash(repo_path: str, hashes_file: Path) -> tuple[bool, str]:
    """
    Verify the current CLAUDE.md hash against the stored value.
    Returns (ok: bool, message: str).
    """
    if not hashes_file.exists():
        return False, "Hash registry not initialized. Run 'conductor register' first."

    hashes = json.loads(hashes_file.read_text())
    if repo_path not in hashes:
        return False, f"No registered hash for {repo_path}. Run 'conductor register'."

    expected = hashes[repo_path]
    actual = compute_claude_md_hash(repo_path)

    if expected == actual:
        return True, "CLAUDE.md hash OK"

    if expected is None and actual is not None:
        return False, f"CLAUDE.md appeared unexpectedly. Hash: {actual}"
    if expected is not None and actual is None:
        return False, f"CLAUDE.md was deleted. Expected hash: {expected}"

    return False, (
        f"CLAUDE.md hash mismatch.\n"
        f"  Expected: {expected}\n"
        f"  Actual:   {actual}\n"
        f"Review changes and re-register with 'conductor register' if the change is legitimate."
    )
```

**Storage format** (in `~/.config/conductor/claude_md_hashes.json`):

```json
{
  "/path/to/repo": "sha256hex",
  "/path/to/other-repo": null
}
```

`null` means "CLAUDE.md is absent at registration time; abort if it appears."

**Integration in conductor pre-dispatch:**

```python
def pre_dispatch_security_check(repo_path: str, hashes_file: Path) -> None:
    """Raise DispatchBlockedError if CLAUDE.md check fails."""
    ok, message = verify_claude_md_hash(repo_path, hashes_file)
    if not ok:
        raise DispatchBlockedError(f"BLOCK: CLAUDE.md integrity check failed: {message}")
```

### 5.3 Hash-Pinning: Limitations

**What hash-pinning does NOT protect against:**

1. **Initial compromise:** If the CLAUDE.md was already adversarial when the operator first registered the hash, the registered hash is also adversarial.
2. **Same-content different-intent attack:** An attacker who knows the expected hash cannot change the file content without triggering detection, but content that looked benign at registration time may have been constructed to trigger malicious behavior via future conditions.
3. **User CLAUDE.md attack surface:** Hash-pinning covers the target repo's project CLAUDE.md. The operator's own `~/.claude/CLAUDE.md` is loaded in every session and is not pinned by this scheme.

**What hash-pinning does protect against:**

1. PR-based CLAUDE.md injection: An adversarial PR that modifies `.claude/CLAUDE.md` will change its hash, triggering detection before the agent is dispatched.
2. Supply chain attack that modifies CLAUDE.md post-clone: Any post-clone modification to the file will be caught before the next dispatch.
3. Branch-specific CLAUDE.md: If a feature branch modifies CLAUDE.md, the hash will differ from the registered value for that repo, alerting the operator.

### 5.4 Storing Hashes Per Branch

For repos where different branches legitimately have different CLAUDE.md content (e.g., a feature branch adds CLAUDE.md content during development), the hash registry should key by `{repo_path}:{branch_name}`:

```json
{
  "/path/to/repo:main": "sha256hex-of-main-claude-md",
  "/path/to/repo:7-my-feature": "sha256hex-of-feature-branch-claude-md"
}
```

On dispatch for branch `7-my-feature`, look up the `{repo}:{branch}` key if present, falling back to `{repo}:main` if no branch-specific hash is registered. This handles the common case where the feature branch has not modified CLAUDE.md and the main hash applies.

---

## Pre-Dispatch CLAUDE.md Review Checklist

This section extends the Pre-Run Security Scan Checklist in `06-security-threat-model.md` with specific CLAUDE.md review automation.

### 6.1 Automated Content Scan

Before spawning any sub-agent, the conductor should scan the target CLAUDE.md for suspicious patterns:

```python
import re

SUSPICIOUS_PATTERNS = [
    # Injection classic patterns
    (r'ignore\s+(?:all\s+)?previous\s+instructions', "CRITICAL: instruction override"),
    (r'SYSTEM\s+OVERRIDE', "CRITICAL: system override claim"),
    (r'you\s+(?:are\s+now|must|shall|should\s+now)\s+(?:ignore|disregard)', "HIGH: authority override"),
    # Exfiltration patterns
    (r'\bcurl\b', "HIGH: curl in CLAUDE.md"),
    (r'\bwget\b', "HIGH: wget in CLAUDE.md"),
    (r'\bnc\b\s', "HIGH: netcat in CLAUDE.md"),
    (r'http[s]?://(?!github\.com|anthropic\.com|code\.claude\.com|platform\.claude\.com)',
     "MEDIUM: external URL not in approved domains"),
    # Scope expansion patterns
    (r'you\s+may\s+(?:now\s+)?(?:modify|edit|write|push)\s+(?:files\s+)?outside',
     "HIGH: scope expansion instruction"),
    (r'push\s+(?:directly\s+)?to\s+main', "CRITICAL: push-to-main instruction"),
    (r'--dangerously-skip-permissions', "HIGH: bypass mode referenced in CLAUDE.md"),
    (r'hooks?.*(?:exfiltrat|steal|send|upload)', "CRITICAL: malicious hook description"),
    # Elevation patterns
    (r'bypass\s+(?:all\s+)?(?:security|restrictions|permissions)', "CRITICAL: bypass instruction"),
    (r'admin(?:istrator)?\s+mode', "HIGH: admin mode claim"),
    (r'elevated\s+(?:permissions?|privileges?|access)', "HIGH: privilege elevation claim"),
]

def scan_claude_md(path: str) -> list[tuple[str, str]]:
    """
    Scan CLAUDE.md for suspicious patterns.
    Returns list of (severity_prefix, description) tuples.
    An empty list means no suspicious patterns found.
    """
    if not os.path.isfile(path):
        return []

    findings = []
    with open(path, "r", errors="replace") as f:
        content = f.read()

    for pattern, description in SUSPICIOUS_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append((description.split(":")[0], f"{description} — pattern: {pattern}"))

    return findings

def pre_dispatch_content_scan(claude_md_path: str) -> None:
    """Raise DispatchBlockedError if CLAUDE.md contains suspicious content."""
    findings = scan_claude_md(claude_md_path)
    critical = [f for f in findings if f[0] == "CRITICAL"]
    high = [f for f in findings if f[0] == "HIGH"]

    if critical:
        raise DispatchBlockedError(
            f"BLOCK: CLAUDE.md contains CRITICAL suspicious patterns:\n" +
            "\n".join(f"  - {f[1]}" for f in critical)
        )
    if high:
        # Log and alert but do not block by default — operator decision
        logger.warning(
            f"WARN: CLAUDE.md contains HIGH-severity suspicious patterns:\n" +
            "\n".join(f"  - {f[1]}" for f in high)
        )
```

### 6.2 Diff-Based Change Detection

A more sophisticated approach compares the current CLAUDE.md against the known-good version (not just hash equality) to produce a human-readable diff for operator review:

```python
import subprocess

def claude_md_diff(path: str, hashes_file: Path) -> str | None:
    """
    Return a unified diff of the current CLAUDE.md vs. the registered version.
    Returns None if the file matches the registered hash.
    """
    ok, _ = verify_claude_md_hash(os.path.dirname(path), hashes_file)
    if ok:
        return None

    # Reconstruct expected content via git show
    try:
        result = subprocess.run(
            ["git", "show", "HEAD:.claude/CLAUDE.md"],
            cwd=os.path.dirname(path),
            capture_output=True, text=True, check=True,
        )
        expected_content = result.stdout
    except subprocess.CalledProcessError:
        return None  # Cannot reconstruct — hash diff is sufficient

    current_content = open(path).read()
    if current_content == expected_content:
        return None

    # Simple line diff
    import difflib
    return "\n".join(difflib.unified_diff(
        expected_content.splitlines(keepends=True),
        current_content.splitlines(keepends=True),
        fromfile="CLAUDE.md (HEAD)",
        tofile="CLAUDE.md (current)",
    ))
```

### 6.3 Complete Pre-Dispatch Checklist

For CLAUDE.md specifically, the conductor pre-dispatch sequence should be:

```
1. [ ] Locate CLAUDE.md: check {repo}/.claude/CLAUDE.md and {repo}/CLAUDE.md
2. [ ] Hash check: compare SHA256 against registered hash → BLOCK on mismatch
3. [ ] Content scan: check for SUSPICIOUS_PATTERNS → BLOCK on CRITICAL, WARN on HIGH
4. [ ] Write claudeMdExcludes: inject settings.local.json into worktree to block target CLAUDE.md
5. [ ] Verify worktree does not inherit adversarial CLAUDE.md from ancestor
6. [ ] Proceed to spawn
```

Steps 4 and 5 are executed regardless of hash check result (defensive layering: even a trusted CLAUDE.md should not be loaded if conductor policy dictates exclusion).

---

## Layered Defense Architecture

Given the absence of a native opt-out flag, the recommended defense-in-depth approach for conductor is:

```
Layer 0: Pre-dispatch conductor-side (before claude -p is invoked)
  ├── SHA256 hash check against registered value
  ├── Automated content scan for suspicious patterns
  └── Diff review alert for changed files

Layer 1: CLAUDE.md loading suppression (affects what claude -p loads)
  ├── claudeMdExcludes in worktree settings.local.json
  │   → blocks target repo CLAUDE.md from loading
  └── Conductor's own CLAUDE.md still loads (desired for orchestration rules)

Layer 2: System prompt override (mitigates loaded CLAUDE.md impact)
  ├── --append-system-prompt-file with conductor's agent-instructions.md
  │   → appended after CLAUDE.md, establishes conductor's authority
  └── Explicit scope framing in the prompt (XML-delimited task)

Layer 3: Tool permission policy (limits damage from malicious CLAUDE.md)
  ├── --allowedTools allowlist (only what the agent needs)
  ├── --disallowedTools denylist (network tools, env dump, force push)
  └── PreToolUse hooks for runtime validation (requires --settings injection)

Layer 4: OS-level sandbox (enforces limits even if CLAUDE.md hijacks tools)
  ├── Filesystem writes restricted to worktree directory
  └── Network blocked except approved domains
```

**Defense completeness assessment:**

| CLAUDE.md attack vector | Layer 0 | Layer 1 | Layer 2 | Layer 3+4 |
|-------------------------|---------|---------|---------|-----------|
| Modified CLAUDE.md (post-clone) | Blocked by hash check | | | |
| PR-injected CLAUDE.md on a feature branch | Blocked by hash check | | | |
| Pre-existing adversarial CLAUDE.md | Content scan may catch | Excludes from load | Reduced influence | Tool limits enforce |
| Zero-day instruction pattern | Missed by scan | Excludes from load | Reduced influence | Tool limits enforce |
| Ancestor repo CLAUDE.md (multi-repo) | Hash check covers known paths | Excludes if configured | Reduced influence | Tool limits enforce |

The most robust configuration excludes the target repo's CLAUDE.md via `claudeMdExcludes` (Layer 1) and passes conductor instructions via `--append-system-prompt-file` (Layer 2). This means the target repo's CLAUDE.md never influences the agent, and the conductor's orchestration rules are the only source of agent behavior.

---

## Cross-References

- **04-configuration.md** — Section 1.2 documents `claudeMdExcludes` as documented for monorepo sibling exclusion. Section 2.2 establishes that `--append-system-prompt-file` is additive to CLAUDE.md (not a replacement), making Layer 2 defense clear. Section 5.4 establishes `CLAUDE_CONFIG_DIR` isolation pattern.
- **06-security-threat-model.md** — Section T2 ("CLAUDE.md Injection") is the threat this document mitigates. Section R-SEC-B spawned this research issue. The Pre-Run Security Scan Checklist in section T2 is extended by section 6.3 of this document.
- **10-settings-mcp-injection.md** — Section 6.2 documents `--setting-sources ""` behavior (which applies to settings files, not CLAUDE.md) and section 6.3 provides the `CLAUDE_CONFIG_DIR` vs. `--setting-sources ""` comparison table.
- **19-pretooluse-reliability.md** — The Layer 3 defense depends on PreToolUse hooks. That document covers whether hooks fire reliably under `--dangerously-skip-permissions`. If hooks are unreliable under bypass mode, Layer 3 is weakened and Layers 1+4 become more critical.

---

## Follow-Up Research Recommendations

### R-18-A: Python Agent SDK CLAUDE.md Loading Control

**Question:** When conductor migrates from subprocess-based `claude -p` to the Python Agent SDK (which only loads CLAUDE.md if `setting_sources=["project"]` is explicitly set), does this provide the desired isolation? Specifically: can conductor load its own orchestration CLAUDE.md via `setting_sources=["user"]` while explicitly NOT loading the target repo's CLAUDE.md by omitting `project` from `setting_sources`?

**Why this matters:** The Python SDK's explicit `setting_sources` parameter is the cleanest opt-out mechanism available — it makes CLAUDE.md loading opt-in rather than opt-out. If the SDK can replace the subprocess approach for conductor's use case, the entire `claudeMdExcludes` / hash-pinning infrastructure becomes simpler or unnecessary.

**Suggested test:** Invoke the Python Agent SDK with `setting_sources=["user"]` from a directory containing a `.claude/CLAUDE.md` and verify via tool call that the project CLAUDE.md content is absent from the agent's context.

**Dependency:** Issue #1 (Agent tool in -p mode) must first establish whether the Python SDK is viable for conductor sub-agents.

### R-18-B: CLAUDE_CODE_SIMPLE Capability Audit for Research Workers

**Question:** With `CLAUDE_CODE_SIMPLE=1`, what exact set of tools is available? Does WebFetch survive? Can `git` be used via Bash? Are there any hooks (for JSONL audit logging) at all? What is the minimal capability set and is it sufficient for research-worker agents?

**Why this matters:** If `CLAUDE_CODE_SIMPLE=1` preserves enough tools for research workers and they don't need hooks-based audit logging, this is the simplest possible CLAUDE.md defense for that agent type.

**Suggested test:**
```bash
CLAUDE_CODE_SIMPLE=1 claude -p --output-format json \
  "List all tools available to you. Do not use any tools, just describe them."
```

### R-18-C: Empirical Test of claudeMdExcludes in -p Mode

**Question:** Does `claudeMdExcludes` in a worktree's `.claude/settings.local.json` actually prevent the named CLAUDE.md from loading when `claude -p` is invoked with that worktree as CWD? Does the glob pattern syntax work correctly for absolute path exclusion?

**Why this matters:** The `claudeMdExcludes` setting is documented as a settings-layer mechanism for interactive monorepo use. Its behavior in headless `-p` mode has not been empirically verified. If it does not apply in `-p` mode, the entire Layer 1 defense collapses.

**Suggested test:**
```bash
# Create a temp dir with a test CLAUDE.md
mkdir -p /tmp/test-repo/.claude
echo "SECRET_INDICATOR_12345" > /tmp/test-repo/.claude/CLAUDE.md

# Create a worktree (or simulate CWD) with claudeMdExcludes
mkdir -p /tmp/test-worktree/.claude
cat > /tmp/test-worktree/.claude/settings.local.json <<EOF
{"claudeMdExcludes": ["/tmp/test-repo/.claude/CLAUDE.md"]}
EOF

# Invoke claude -p from the worktree with the test repo as ancestor
# (by setting the parent of the worktree to the test-repo path)
# Check whether "SECRET_INDICATOR_12345" appears in the agent's context
```

### R-18-D: Worktree Boundary Traversal Fix Status

**Question:** Has GitHub issue #16600 (memory traversal should respect git worktree boundaries) been resolved or had a workaround released? What is the definitive behavior on the latest Claude Code version regarding `.git` file vs. `.git` directory boundary detection?

**Why this matters:** If traversal has been fixed to stop at worktree boundaries, conductor no longer needs `claudeMdExcludes` for same-repo worktrees. If it remains broken, `claudeMdExcludes` is the only mitigation.

**Reference:** Issue #16600 was open as of March 1, 2026 with related issues #23565 (marked NOT_PLANNED), #24283, #24382, #20880, and #26944 (marked DUPLICATE).

### R-18-E: ConfigChange Hook for CLAUDE.md Write Detection

**Question:** The `ConfigChange` hook fires when "a configuration file changes during a session." Does this include changes to CLAUDE.md files? If yes, conductor could deploy a `ConfigChange` hook that blocks CLAUDE.md modifications during the session, preventing a running agent from writing a new adversarial CLAUDE.md into the working directory.

**Why this matters:** If an agent is compromised mid-session and attempts to write a modified CLAUDE.md to influence future agents (the cascading failure scenario from T7), a `ConfigChange` hook could detect and block this.

**Reference:** The hooks documentation lists `ConfigChange` with matcher values `user_settings`, `project_settings`, `local_settings`, `policy_settings`, `skills`. CLAUDE.md files are not settings files, so this hook may not cover them. Empirical verification needed.

---

## Sources

- [How Claude remembers your project — Claude Code Docs](https://code.claude.com/docs/en/memory) — CLAUDE.md resolution hierarchy, `claudeMdExcludes` exact syntax, load order, managed policy exception, `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`
- [Claude Code Settings — Claude Code Docs](https://code.claude.com/docs/en/settings) — `CLAUDE_CODE_SIMPLE` environment variable: "Disables MCP tools, attachments, hooks, and CLAUDE.md files." Full env var catalogue.
- [Security — Claude Code Docs](https://code.claude.com/docs/en/security) — Trust verification disabled in `-p` mode (buried sub-bullet under Trust Verification)
- [Configure Permissions — Claude Code Docs](https://code.claude.com/docs/en/permissions) — `allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`, managed settings hierarchy
- [Hooks Reference — Claude Code Docs](https://code.claude.com/docs/en/hooks) — `SessionStart`, `ConfigChange`, `PreToolUse` hook event schemas; hook lifecycle diagram
- [Run Claude Code programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless) — Headless mode documentation (confirms no `--no-project-md` flag)
- [Modifying system prompts — Claude Agent SDK Docs](https://platform.claude.com/docs/en/agent-sdk/modifying-system-prompts) — `setting_sources` parameter for SDK; CLAUDE.md only loads if `setting_sources=["project"]` is set
- [GitHub Issue #20253: Security-critical -p flag behavior (trust verification disabled) — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/20253) — Closed not planned February 28, 2026. Trust verification bypass in -p mode confirmed undocumented.
- [GitHub Issue #16600: Memory traversal should respect git worktree boundaries — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/16600) — Open as of March 2026. Root cause: traversal does not stop at `.git` file boundary.
- [GitHub Issue #23565: Memory files loaded twice in git worktrees — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/23565) — Marked NOT_PLANNED.
- [GitHub Issue #20880: Exclude parent CLAUDE.md files from auto-loading — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/20880) — Open, no milestone.
- [Research doc 04-configuration.md — breadmin-conductor](../research/04-configuration.md) — CWD semantics, CLAUDE.md resolution order, `claudeMdExcludes` first mention, `CLAUDE_CONFIG_DIR` isolation pattern
- [Research doc 06-security-threat-model.md — breadmin-conductor](../research/06-security-threat-model.md) — T2: CLAUDE.md Injection threat definition and mitigations table; Pre-Run Security Scan Checklist
- [Research doc 10-settings-mcp-injection.md — breadmin-conductor](../research/10-settings-mcp-injection.md) — `--setting-sources` flag behavior, `CLAUDE_CONFIG_DIR` vs. `--setting-sources ""` comparison, per-agent settings injection pattern
- [Claude Code Worktrees and Configuration — BloggingAbout.NET (February 2026)](https://bloggingabout.net/2026/02/20/claude-code-worktrees-and-configuration/) — Worktree configuration context
- [Claude-code-config — Trail of Bits (GitHub)](https://github.com/trailofbits/claude-code-config) — Security model using sandboxing + PreToolUse hooks as primary defenses; no CLAUDE.md-specific protections implemented
- [Detecting Indirect Prompt Injection in Claude Code with Lasso — Lasso Security](https://www.lasso.security/blog/the-hidden-backdoor-in-claude-coding-assistant) — Indirect injection via tool outputs (PostToolUse hook pattern); confirmed no CLAUDE.md-specific detection
- [lasso-security/claude-hooks — GitHub](https://github.com/lasso-security/claude-hooks) — Prompt injection detection patterns; no CLAUDE.md hash-pinning or integrity check
