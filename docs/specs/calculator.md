# Spec: Calculator CLI

## Overview

A command-line calculator that evaluates arithmetic expressions passed as arguments and
prints the result to stdout. Targets developers and power users who want quick calculations
without leaving the terminal. Supports the four basic arithmetic operations with standard
operator precedence and parenthesized grouping. This tool serves as the Tier 1
proof-of-concept for validating the breadmin-composer pipeline end-to-end.

---

## Success Criteria

- [ ] `calc '2 + 3'` prints `5` to stdout and exits 0
- [ ] `calc '10 / 4'` prints `2.5` to stdout and exits 0
- [ ] `calc '2 + 3 * 4'` prints `14` to stdout and exits 0 (multiplication before addition)
- [ ] `calc '(2 + 3) * 4'` prints `20` to stdout and exits 0 (parentheses override precedence)
- [ ] `calc '4 / 2'` prints a result without a trailing `.0` (i.e., `2`, not `2.0`)
- [ ] `calc '1 / 0'` prints an error message to stderr and exits non-zero
- [ ] `calc` with no arguments prints usage to stderr and exits non-zero
- [ ] `calc '2 +'` (malformed expression) prints an error message to stderr and exits non-zero

---

## Scope

**Included:**

- Parsing and evaluating arithmetic expressions with `+`, `-`, `*`, `/`
- Operator precedence (`*` and `/` before `+` and `-`)
- Parentheses for grouping
- Integer and floating-point number literals
- Integer output when the result has no fractional part (e.g., `4 / 2` → `2`)
- Error output on division by zero or malformed input
- Single-argument invocation: `calc '<expression>'`

---

## Constraints

- Must run on macOS and Linux without external runtime dependencies beyond the standard library
- Output must be a single line containing only the result (or an error message)
- No configuration files — all input via CLI argument
- Must complete any valid expression in under 100 ms on commodity hardware

---

## Key Unknowns

1. Which expression parsing strategy — recursive descent parser, operator-precedence
   (Pratt/shunting-yard), or delegating to a sandboxed `eval` — best fits the simplicity
   and safety constraints for this tool? The answer determines the core implementation
   approach for the parser module.

---

## Non-Goals

- Variables and assignments (e.g., `x = 5`)
- User-defined functions
- Trigonometric, logarithmic, or other mathematical functions
- Graphing or plotting
- Interactive REPL mode
- Expression history or session state
- GUI or web interface
- Windows support in this version
