"""Bounded JSON interchange for a manual slate; no filesystem paths or code execution."""

from __future__ import annotations

import json

SCHEMA_VERSION = 2
MAX_JSON_BYTES = 131072
ROOT_KEYS_V1 = {"schema_version", "season", "week", "sportsbook", "captured_at", "prices", "rows"}
ROOT_KEYS_V2 = ROOT_KEYS_V1 | {"league", "source", "line_label"}
ROW_KEYS = {"away_team", "home_team", "team", "spread", "total", "kickoff"}


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def decode_slate(data: bytes) -> dict:
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON file exceeds 128 KiB")
    try:
        slate = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                           parse_float=str, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid UTF-8 JSON slate") from exc
    if not isinstance(slate, dict) or type(slate.get("schema_version")) is not int:
        raise ValueError("unsupported slate schema or fields")
    version = slate["schema_version"]
    if (version == 1 and set(slate) != ROOT_KEYS_V1) or (version == 2 and set(slate) != ROOT_KEYS_V2) or version not in {1, 2}:
        raise ValueError("unsupported slate schema or fields")
    if version == 2 and (slate["league"] not in {"NFL", "CFB"} or
                         slate["source"] not in {"manual_sportsbook", "user_screenshot", "reference_source", "future_odds_api"} or
                         slate["line_label"] not in {"current_pregame", "archived_pregame_reference", "true_timestamped_pregame"}):
        raise ValueError("invalid league or provenance")
    if not isinstance(slate["rows"], list) or len(slate["rows"]) > 64:
        raise ValueError("slate must have at most 64 sides")
    if any(not isinstance(row, dict) or set(row) != ROW_KEYS for row in slate["rows"]):
        raise ValueError("each side must have exactly the supported fields")
    if not isinstance(slate["prices"], dict) or set(slate["prices"]) != {"2", "3"}:
        raise ValueError("prices must have 2 and 3 fields")
    return slate


def encode_slate(slate: dict) -> bytes:
    return json.dumps(slate, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def slate_fingerprint(slate: dict) -> bytes:
    return json.dumps(slate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
