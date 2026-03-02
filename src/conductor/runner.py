"""Headless claude -p runner.

Invokes `claude -p` as a subprocess with stream-json output,
captures cost/usage events, and yields text output.
"""

from __future__ import annotations

# TODO (issue #I3): implement async subprocess runner with stream-json capture
