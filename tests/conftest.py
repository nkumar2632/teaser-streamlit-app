"""Keep every test away from the operator's real local market data."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_default_history(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("TEASER_DATA_DIR", str(tmp_path_factory.mktemp("default_market_data")))
