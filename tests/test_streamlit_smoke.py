"""End-to-end Streamlit rendering against the real pinned Week 2 model."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_blank_app_and_week2_historical_mobile_hierarchy():
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    assert not app.exception
    assert any("Enter a sportsbook slate" in item.value for item in app.info)

    next(button for button in app.button if button.label == "Load verified Week 2 historical example").click().run()
    assert not app.exception
    assert [heading.value for heading in app.subheader[:2]] == [
        "Proposed card", "Qualifying PRIMARY / LIVE legs",
    ]
    assert any("HISTORICAL EXAMPLE" in item.value for item in app.warning)
    assert any("SHADOW — NOT PLACED" in item.value for item in app.markdown)
    assert any("+170" in item.value and "EV +12.7%" in item.value for item in app.markdown)
    assert any("TB 2u" in item.value and "BAL 2u" in item.value for item in app.markdown)
    recheck = next(button for button in app.button if button.label == "Recheck selected card against current input")
    assert recheck.disabled
