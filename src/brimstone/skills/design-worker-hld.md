Write the High-Level Design (HLD) document for a milestone and file LLD issues for each module.

## When to Run

Dispatched by design-worker Phase 1 after all research issues for the milestone are closed.

## Inputs

From the session prompt:
- `Repository` — `owner/repo` of the target GitHub repo
- `Milestone` — the version being designed (e.g. `calculator-v0.1.x`)
- `Issue` — the `Design: HLD for <milestone>` GitHub issue number
- `Branch` — the branch to commit to

## Execution

### Step 0 — Check Out Branch

```bash
DEFAULT_BRANCH=$(gh repo view --repo <owner>/<repo> --json defaultBranchRef --jq '.defaultBranchRef.name')
git fetch origin
git checkout <branch>
git rebase origin/$DEFAULT_BRANCH
```

### Step 1 — Read Research Docs

Fetch and read every research doc for this milestone in `docs/research/<milestone>/`:

```bash
# List research docs for this milestone
gh api repos/<owner>/<repo>/contents/docs/research/<milestone> --jq '.[].name'

# Read each doc
gh api repos/<owner>/<repo>/contents/docs/research/<milestone>/<filename> --jq '.content' | base64 -d
```

Also read the spec and CLAUDE.md:

```bash
gh api repos/<owner>/<repo>/contents/docs/specs/<version>.md --jq '.content' | base64 -d
gh api repos/<owner>/<repo>/contents/CLAUDE.md --jq '.content' | base64 -d
```

### Step 2 — Write the HLD

Create `docs/design/<milestone>/HLD.md` in the local checkout.

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
git commit -m "docs: add HLD for <milestone> (Closes #<issue_number>)"
git push
```

### Step 4 — File LLD Issues

For each module identified in the HLD, check for a duplicate then file a `stage/design` issue:

```bash
# Fetch existing issue titles to check for dups
EXISTING=$(gh issue list --repo <owner>/<repo> --state all --limit 500 --json title --jq '.[].title')

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
