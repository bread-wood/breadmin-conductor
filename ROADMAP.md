# brimstone Roadmap

Each milestone from v0.2.0 onward is self-hosted — brimstone develops brimstone.

## v0.1.0 — Beads Foundation [SHIPPED]

Core state persistence via bead files; Watchdog (zombie recovery); MergeQueue; agent PR ownership.

- BeadStore, WorkBead, PRBead, MergeQueue
- `_process_merge_queue()` — sequential squash merge
- `_watchdog_scan()` — zombie agent recovery (renamed from _deacon_scan in v0.1.1)
- Agent PR ownership (CI max 3 + reviews max 2)
- Checkpoint schema v3

## v0.1.1 — Self-Hosting Ready [PLANNED]

Bug fixes, docs, and pipeline validation on calculator before brimstone self-hosts.

- [x] Fix #226: scope/research crash when model == FALLBACK_MODEL
- [x] Fix #227: scope agent dependency wiring (gh issue view append pattern)
- [x] Rename Deacon → Watchdog
- [ ] Docs rewrite (HLD + LLDs for beads, cli, session; research reorganized)
- [ ] README rewrite
- [ ] ROADMAP.md

### v0.1.1 Validation — Full Pipeline on calculator

Run brimstone against bread-wood/calculator:

```bash
brimstone run --stage scope --repo bread-wood/calculator --milestone v0.2.0
brimstone run --stage impl  --repo bread-wood/calculator --milestone v0.2.0
```

Success criteria:
- No human intervention required after kickoff
- Total spend < $5 for one milestone
- All PRs merge cleanly via MergeQueue
- `brimstone` accurately reflects shipped vs in-progress issues

## v0.2.0 — Hardened Core [PLANNED — first self-hosted milestone]

Credential proxy, GHA deployment on issue events, improved crash recovery, bead visualizer.
Developed using brimstone on bread-wood/brimstone itself.

- Bead visualizer: `brimstone beads` renders a live terminal view of WorkBead/PRBead state
  across all issues in the current campaign (campaign progress, per-issue state, PR CI status)

## v0.3.0 — Multi-Model [PLANNED]

Gemini CLI backend, cross-model consensus for design docs, model routing by task type.
