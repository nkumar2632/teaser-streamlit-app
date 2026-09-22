from __future__ import annotations

import ast
import json
import sys
from types import ModuleType
from pathlib import Path

import pytest

from teaser_app.inputs import MAX_JSON_BYTES, decode_slate
from teaser_app.presentation import h, percent, signed
from teaser_app.integrity import ModelIntegrityError, verify_model

ROOT = Path(__file__).resolve().parents[1]


def test_ui_never_imports_source_model_or_calculates_model_values():
    for path in [ROOT / "app.py", *(ROOT / "teaser_app" / name for name in
                    ("inputs.py", "presentation.py", "views.py", "cfb_page.py", "market_data.py", "paper_history.py", "strategy.py", "url_ingest.py", "url_import.py", "providers/espn.py"))]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("teaser_model_v1")
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("teaser_model_v1") for alias in node.names)
        names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not names & {"p_est", "p_raw", "sigma", "generate_tickets", "select_live_tickets", "select_top_legs", "ev_per_unit", "ticket_probability"}


def test_input_json_rejects_duplicate_fields_extra_fields_and_oversize():
    with pytest.raises(ValueError, match="duplicate"):
        decode_slate(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="schema"):
        decode_slate(b'{"schema_version":1,"extra":1}')
    with pytest.raises(ValueError, match="128 KiB"):
        decode_slate(b" " * (MAX_JSON_BYTES + 1))
    with pytest.raises(ValueError, match="schema"):
        decode_slate(b'{"schema_version":true,"season":2026,"week":2,"sportsbook":"x","captured_at":"x","prices":{"2":"","3":""},"rows":[]}')


def test_signed_display_and_html_escape():
    assert signed("2.5") == "+2.5"
    assert signed("-8.5") == "-8.5"
    assert signed("170") == "+170"
    assert percent("0.12699437773262068", sign=True) == "+12.7%"
    assert h('<img src=x onerror=alert(1)>') == '&lt;img src=x onerror=alert(1)&gt;'
    assert signed("unvalidated entry") == "unvalidated entry"
    assert signed("nan") == "nan"


def test_foreign_pathless_model_module_is_refused():
    name = "teaser_model_v1.fake_foreign_module"
    assert name not in sys.modules
    sys.modules[name] = ModuleType(name)
    try:
        with pytest.raises(ModelIntegrityError, match="foreign model module"):
            verify_model()
    finally:
        del sys.modules[name]
    verify_model()
