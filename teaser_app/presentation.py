"""Display-only formatting and small escaped mobile cards."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape


def signed(value: object) -> str:
    raw = str(value)
    if raw in {"", "UNAVAILABLE"}:
        return "—"
    try:
        number = Decimal(raw)
    except InvalidOperation:
        # Keep malformed manual input visible so validation can reject it on Build.
        return raw
    if not number.is_finite():
        return raw
    return f"+{raw}" if number > 0 and not raw.startswith("+") else raw


def percent(value: object, digits: int = 1, *, sign: bool = False) -> str:
    if str(value) in {"", "UNAVAILABLE"}:
        return "—"
    number = float(value) * 100
    return f"{number:+.{digits}f}%" if sign else f"{number:.{digits}f}%"


def h(value: object) -> str:
    return escape(str(value), quote=True)


def card(title: str, eyebrow: str, body: str, *, accent: str = "") -> str:
    # title, eyebrow and body must already be escaped by the caller.
    return (
        f'<div class="teaser-card {accent}">'
        f'<div class="teaser-eyebrow">{eyebrow}</div>'
        f'<div class="teaser-title">{title}</div>'
        f'<div class="teaser-body">{body}</div>'
        '</div>'
    )


def book_name(book: object) -> str:
    raw = str(book or "").strip()
    return raw[:1].upper() + raw[1:]


def source_label(snapshot: dict) -> str:
    """Operator-facing name of a saved market snapshot's source."""
    from teaser_app.market_data import snapshot_role

    if snapshot.get("source_type") == "screenshot":
        return f"SPORTSBOOK SNAPSHOT — {book_name(snapshot.get('sportsbook')) or 'unknown sportsbook'}"
    if snapshot_role(snapshot) == "REFERENCE":
        return f"REFERENCE — {snapshot.get('source_provider') or book_name(snapshot.get('sportsbook')) or 'ESPN'}"
    return f"EXECUTION — {book_name(snapshot.get('sportsbook')) or 'unknown sportsbook'}"
