# [Product] [Version] — [Theme]

> **Who reads this file and why**
>
> This spec is the authoritative statement of *what* the product does and *why* it exists.
> It is consumed by:
> - **init** — seeds the milestone and derives the research queue from Key Unknowns
> - **research** — investigates Key Unknowns; findings constrain design choices
> - **design** — uses Scope, Constraints, and Failure Modes to bound the HLD/LLD
> - **scoping** — uses Success Criteria to write acceptance criteria for each impl issue
> - **qa** — runs every Success Criterion and every Failure Mode as a test case
> - **human reviewers** — uses Non-Goals to prevent scope creep
>
> Write this file before running `composer init`. Keep it product-focused (what and why),
> not technical (not how). The how belongs in design docs.

---

## Overview

> 2–4 sentences. What does this product do, who is it for, and why does it exist now?
> Avoid implementation details. Focus on user value and the problem being solved.

[Replace with your overview paragraph.]

---

## Success Criteria

> Binary, testable, observable. Each criterion is independently verifiable and becomes
> a qa test case. Use exact commands, exact outputs, exact exit codes.
>
> Good: `` `calc '2 + 3'` prints `5` to stdout and exits 0 ``
> Bad: "Calculator should handle basic arithmetic"
>
> Avoid "should" and "may" — use exact observable outcomes. If you can't write a test
> for it, it doesn't belong here.

- [ ] [Criterion: exact command → exact observable output + exit code]
- [ ] [Criterion: exact command → exact observable output + exit code]

---

## Scope

> What IS included in this version. Be explicit — if it's not listed, agents will not
> build it. Use noun phrases, not sentences.

**Included:**

- [Feature or capability]
- [Feature or capability]

---

## Failure Modes

> The lessons from v0.1: most bugs live at boundaries — between processes, between services,
> between components. For each major boundary this product touches, specify what happens
> when it fails. These become qa test cases alongside Success Criteria.
>
> Format: **Condition** → expected behavior (exit code, output, side effects).

**Input validation:**
- [Invalid input type] → [expected error output to stderr, exit code]
- [Missing required argument] → [expected error output to stderr, exit code]

**External service failures:**
- [Service X unavailable] → [expected behavior: retry? fail fast? degrade gracefully?]
- [Auth failure] → [expected error message, no partial state left behind]

**State / persistence failures:**
- [Corrupt or missing state file] → [expected recovery behavior]
- [Partial write / crash mid-operation] → [expected recovery on next run]

**Subprocess / process failures:** *(if applicable)*
- [Subprocess exits non-zero] → [expected orchestrator behavior]
- [Subprocess hangs / no output] → [expected timeout and cleanup behavior]

---

## Interface Contracts

> For CLI tools and APIs: the exact contract at every boundary. This is what the qa stage
> tests against. Omit this section for non-CLI/API products.

**CLI:**

```
product-name <required-arg> [--optional-flag VALUE]
```

| Stream | Content |
|--------|---------|
| stdout | [Exact format of successful output] |
| stderr | [Exact format of error messages] |
| exit 0 | [Condition for success] |
| exit 1 | [Condition for user error / invalid input] |
| exit 2 | [Condition for system error / external failure] |

**Environment variables:** *(if any)*

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRODUCT_FOO` | Yes | [What it controls] |

---

## Constraints

> Hard limits the implementation must respect. Non-negotiable. Examples: platform targets,
> performance budgets, dependency restrictions, backward-compatibility requirements.

- [Constraint]
- [Constraint]

---

## Key Unknowns

> Only questions where the *answer changes the design*. If knowing the answer wouldn't
> change what you build or how you build it, omit it. Keep to 1–3 maximum.
>
> Each unknown becomes a `stage/research` issue. Assign a priority hint:
> - **P1** — must be resolved before design begins (blocking)
> - **P2** — should be resolved before scoping begins (important but not blocking)
>
> Write as a question whose answer determines an implementation approach.

1. **[P1]** [Question: answer changes the core architecture or a major design decision]
2. **[P2]** [Question: answer changes a module-level design but not the overall architecture]

---

## Non-Goals

> Explicitly excluded from this version. This prevents scope creep and tells agents what
> NOT to implement. Be specific — vague exclusions get ignored.

- [Excluded feature]
- [Excluded feature]

---

## Example: Calculator CLI v0.1.x

> Complete filled example. Delete when writing a real spec.

---

# Calculator CLI v0.1.x — Cold Start

## Overview

A command-line calculator that evaluates arithmetic expressions passed as arguments and
prints the result to stdout. Targets developers who want quick calculations without leaving
the terminal. Supports the four basic operations with standard operator precedence and
parenthesized grouping. Serves as the Tier 1 proof-of-concept for the breadmin-composer
pipeline.

---

## Success Criteria

- [ ] `calc '2 + 3'` prints `5` to stdout and exits 0
- [ ] `calc '10 / 4'` prints `2.5` to stdout and exits 0
- [ ] `calc '2 + 3 * 4'` prints `14` to stdout and exits 0
- [ ] `calc '(2 + 3) * 4'` prints `20` to stdout and exits 0
- [ ] `calc '4 / 2'` prints `2`, not `2.0` (no trailing `.0` when result is a whole number)
- [ ] `calc '1 / 0'` prints `error: division by zero` to stderr and exits 1
- [ ] `calc` with no arguments prints usage to stderr and exits 1
- [ ] `calc '2 +'` prints `error: unexpected end of expression` (or similar) to stderr and exits 1

---

## Scope

**Included:**

- Arithmetic: `+`, `-`, `*`, `/`
- Operator precedence (`*` and `/` before `+` and `-`)
- Parentheses for grouping
- Integer and floating-point number literals
- Clean integer output when result has no fractional part
- Single-argument invocation: `calc '<expression>'`

---

## Failure Modes

**Input validation:**
- No arguments → print usage to stderr, exit 1
- More than one argument → print `error: expected a single quoted expression` to stderr, exit 1
- Empty string argument → print `error: empty expression` to stderr, exit 1
- Malformed expression (e.g. `2 +`) → print `error: <description>` to stderr, exit 1
- Division by zero → print `error: division by zero` to stderr, exit 1
- Number out of representable range → print `error: overflow` to stderr, exit 1

**No external service or subprocess boundaries for this product.**

---

## Interface Contracts

**CLI:**

```
calc '<expression>'
```

| Stream | Content |
|--------|---------|
| stdout | The result as a number (integer if whole, decimal otherwise), followed by a newline |
| stderr | `error: <description>` followed by a newline, on any failure |
| exit 0 | Expression evaluated successfully |
| exit 1 | Any error: malformed input, division by zero, wrong number of arguments |

---

## Constraints

- Runs on macOS and Linux; no external runtime dependencies beyond the standard library
- Output is a single line: the result or an error message
- No configuration files — all input via CLI argument
- Completes any valid expression in under 100 ms on commodity hardware

---

## Key Unknowns

1. **[P1]** Which parsing strategy — recursive descent, Pratt/shunting-yard, or sandboxed
   `eval` — best fits the simplicity and safety constraints? The answer determines the
   core architecture of the parser module and whether `eval` is acceptable.

---

## Non-Goals

- Variables and assignments (`x = 5`)
- User-defined functions
- Trigonometric, logarithmic, or other functions
- Interactive REPL mode
- Expression history or session state
- GUI or web interface
- Windows support in this version
