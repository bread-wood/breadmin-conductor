# Spec: [Product Name]

> **Who reads this file and why**
>
> This spec is the authoritative statement of *what* the product does and *why* it exists.
> It is consumed by:
> - **research-worker** — derives research questions from Key Unknowns
> - **design-worker** — uses Scope and Constraints to bound the HLD/LLD
> - **plan-milestones** — uses Success Criteria to determine when the milestone is complete
> - **human reviewers** — uses Non-Goals to prevent scope creep
>
> Write this file before filing any milestone issues. Keep it product-focused (what and why),
> not technical (not how).

---

## Overview

> **Who reads this**: Everyone. This is the one-paragraph elevator pitch.
>
> Write 2-4 sentences describing what this product does and who it is for.
> Avoid implementation details. Focus on user value.

[Replace with your overview paragraph.]

---

## Success Criteria

> **Who reads this**: plan-milestones (seeds impl acceptance criteria), QA, human reviewers.
>
> List binary, testable criteria. Each criterion must be independently verifiable.
> Avoid "should" and "may" — use "must" or define an exact observable outcome.
> Good: "`calc '2 + 3'` prints `5` to stdout and exits 0"
> Bad: "Calculator should handle basic arithmetic"

- [ ] [Criterion 1: exact, binary, testable]
- [ ] [Criterion 2: exact, binary, testable]
- [ ] [Add more as needed]

---

## Scope

> **Who reads this**: design-worker (bounds the HLD), impl agents (defines what they may build).
>
> List what IS included in this version. Be explicit. If it's not listed here, agents will not build it.
> Use noun phrases, not sentences.

**Included:**

- [Feature or capability 1]
- [Feature or capability 2]
- [Add more as needed]

---

## Constraints

> **Who reads this**: design-worker (technical boundaries), impl agents (must-follow rules).
>
> List hard constraints the implementation must respect. These are non-negotiable requirements
> that limit how the product can be built. Examples: platform targets, performance budgets,
> dependency restrictions, regulatory requirements.

- [Constraint 1]
- [Constraint 2]
- [Add more as needed]

---

## Key Unknowns

> **Who reads this**: research-worker (sources its research queue directly from this section).
>
> List only questions where the *answer changes the design*. If knowing the answer wouldn't
> change what you build or how you build it, omit it. Keep to 1-3 questions maximum.
>
> Each unknown will become a `stage/research` issue. Write them as questions.

1. [Question 1: phrase as a question whose answer changes the implementation approach]
2. [Question 2: optional, only if genuinely design-blocking]

---

## Non-Goals

> **Who reads this**: Everyone. This is the explicit "we are not building X" list.
>
> Enumerate features explicitly excluded from this version. This prevents scope creep
> and tells agents what NOT to implement. Be specific.

- [Excluded feature 1]
- [Excluded feature 2]
- [Add more as needed]

---

## Example: Calculator CLI

> The section below is a complete filled example. Delete it when writing a real spec.

---

# Spec: Calculator CLI

## Overview

A command-line calculator that evaluates arithmetic expressions passed as arguments and
prints the result. Targets developers and power users who want quick calculations without
leaving the terminal. Supports the four basic operations and respects standard operator
precedence.

---

## Success Criteria

- [ ] `calc '2 + 3'` prints `5` to stdout and exits 0
- [ ] `calc '10 / 4'` prints `2.5` to stdout and exits 0
- [ ] `calc '2 + 3 * 4'` prints `14` to stdout (precedence: multiplication before addition)
- [ ] `calc '1 / 0'` prints an error message to stderr and exits non-zero
- [ ] `calc` with no arguments prints usage to stderr and exits non-zero
- [ ] Invalid expressions (e.g., `calc '2 +'`) print an error to stderr and exit non-zero

---

## Scope

**Included:**

- Parsing and evaluating arithmetic expressions: `+`, `-`, `*`, `/`
- Operator precedence (`*` and `/` before `+` and `-`)
- Parentheses for grouping: `calc '(2 + 3) * 4'`
- Integer and floating-point number literals
- Error output on division by zero or malformed expressions
- Single-argument invocation: `calc '<expression>'`

---

## Constraints

- Must run on macOS, Linux, and Windows without external runtime dependencies
- Output must be a single line containing only the result (or an error message)
- No configuration files — all input via CLI argument
- Must complete any valid expression in under 100 ms on commodity hardware

---

## Key Unknowns

1. Should the result be printed as an integer when the fractional part is zero
   (e.g., should `calc '4 / 2'` print `2` or `2.0`)? The answer determines the
   output formatting logic and affects the Success Criteria wording.

---

## Non-Goals

- Variables and assignments (e.g., `x = 5`)
- User-defined functions
- Trigonometric or logarithmic functions
- Graphing or plotting
- Interactive REPL mode
- Expression history
- GUI or web interface
