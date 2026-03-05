Write the High-Level Design (HLD) document for a milestone and file LLD issues for each module.

**Do NOT use the Agent tool. Do NOT spawn sub-agents.**

## When to Run

Dispatched by design-worker Phase 1 after all research issues for the milestone are closed.

## Inputs

From the session prompt:
- `Repository` — `owner/repo` of the target GitHub repo
- `Milestone` — the version being designed (e.g. `calculator-v0.1.x`)
- `Issue` — the `Design: HLD for <milestone>` GitHub issue number
- `Branch` — the branch to commit to
- `Working Directory` — absolute path to the isolated worktree (already checked out)

## Execution

### Step 0 — Enter Your Working Directory

```bash
cd <Working Directory from Session Parameters>
```

The branch is already checked out. Do NOT run `git checkout` or `git rebase`.

### Step 1 — Read Research Docs and Spec

Read every research doc for this milestone from your working directory:

```bash
# List and read all research docs
ls docs/research/<milestone>/
cat docs/research/<milestone>/*.md

# Read the spec
cat docs/specs/*.md

# Read CLAUDE.md if it exists
cat CLAUDE.md 2>/dev/null || true
```

### Step 1.5 — Read the Previous HLD (skip only for the very first milestone)

```bash
# List all existing design milestones in sorted order
ls docs/design/ 2>/dev/null | sort -V

# Read the most recent prior HLD
cat docs/design/<previous-milestone>/HLD.md 2>/dev/null \
  || echo "No prior HLD found — this is the first milestone."
```

You will integrate the prior HLD in Step 2.

### Step 2 — Write the HLD

Create `docs/design/<milestone>/HLD.md` in the local checkout.

**The HLD is a complete system snapshot, never a delta.**

- **First milestone only** (no prior HLD exists): write from scratch using the sections below.
- **All subsequent milestones**: read the previous HLD (Step 1.5), then produce a
  **complete system snapshot** for the current version. Pull forward all retained
  architecture, design decisions, module descriptions, cross-cutting concerns, and
  data flow from the previous HLD. Integrate the new additions throughout.
  A reader must be able to understand the entire current system from this document
  alone — without consulting any prior version.

**Common mistakes to avoid:**
- Do NOT write "v0.2.0 adds X and Y" and stop — describe the full system at this version.
- Do NOT omit foundational design decisions from earlier milestones; carry them forward.
- Do NOT write the System Overview as a list of changes from the last version; describe the system as it exists now.

The HLD must cover:

**System overview**
- One-paragraph description of what is being built and why
- Key constraints and non-goals (from spec)

**Architecture**
- Top-level component diagram (ASCII is fine)
- Data flow between components
- Key design decisions and rationale (reference the research docs that informed each decision)

**Module breakdown**
This section is critical — it defines the LLD scope. For each module:

```
### Module: <name>

**Responsibility**: one sentence
**Key interfaces**: what it exposes to other modules
**Files**: which source files it owns
**Dependencies**: which other modules it depends on
```

List every module that needs a Low-Level Design document. Use the module names exactly as they will appear in `docs/design/<milestone>/lld/<module>.md`.

**Cross-cutting concerns**
- Error handling strategy
- Testing approach at the system level
- Configuration and environment
- Observability / logging

**Open questions**
Any design decisions deferred to LLD.

### Step 3 — Commit the HLD

```bash
git add docs/design/<milestone>/HLD.md
git commit -m "docs: add HLD for <milestone> (Closes #<issue_number>) [skip ci]"
git push -u origin <branch>
```

### Step 4 — File LLD Issues

For each module identified in the HLD, check for a duplicate then file a `stage/design` issue:

```bash
# Fetch existing issue titles scoped to this milestone to check for dups
# IMPORTANT: scope to --milestone so closed issues from prior milestones
# (e.g. "Design: LLD for lexer" from v0.1.0) don't suppress creation here.
EXISTING=$(gh issue list --repo <owner>/<repo> --state all --milestone "<milestone>" --limit 500 --json title --jq '.[].title')

for MODULE in <module1> <module2> ...; do
  TITLE="Design: LLD for $MODULE"
  if echo "$EXISTING" | grep -qxF "$TITLE"; then
    echo "Issue '$TITLE' already exists — skipping"
  else
    gh issue create \
      --repo <owner>/<repo> \
      --title "$TITLE" \
      --label "stage/design" \
      --milestone "<milestone>" \
      --body "$(cat <<EOF
## Deliverable
\`docs/design/lld/$MODULE.md\`

## Inputs
- HLD: \`docs/design/<milestone>/HLD.md\`
- Research docs: \`docs/research/<milestone>/\`

## Acceptance Criteria
The LLD must cover: data structures, key algorithms, public API/interfaces, error
handling, and test strategy for this module.
EOF
)"
  fi
done
```

### Step 5 — Create PR

```bash
DEFAULT_BRANCH=$(gh repo view --repo <owner>/<repo> --json defaultBranchRef --jq '.defaultBranchRef.name')
gh pr create \
  --repo <owner>/<repo> \
  --title "Design: HLD for <milestone>" \
  --body "Closes #<issue_number>" \
  --base $DEFAULT_BRANCH
```

### Step 6 — STOP

Do not merge. The orchestrator monitors the PR and merges when CI passes.

## Constraints

- **One PR, one doc**: this agent produces only `docs/design/<milestone>/HLD.md`
- **Milestone-scoped paths**: all paths use `<milestone>` as the directory name, not a generic folder
- **LLD issues use exact module names**: the names here become file paths
  (`docs/design/<milestone>/lld/<module>.md`) and must be filesystem-safe
- **No implementation code**: this agent writes documentation only
- **Check for dup LLD issues** before filing — re-runs must be safe
