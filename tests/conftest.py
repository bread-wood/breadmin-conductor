"""Root conftest — patches that apply to every test in the suite."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_sleep():
    """Replace time.sleep with a no-op so stall loops don't slow tests."""
    with patch("brimstone.cli.time.sleep"):
        yield
