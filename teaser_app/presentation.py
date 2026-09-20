"""Display-only formatting and small escaped mobile cards."""

from __future__ import annotations

from decimal import Decimal
from html import escape


def signed(value: object) -> str:
    raw = str(value)
    if raw in {"", "UNAVAILABLE"}:
        return "—"
    number = Decimal(raw)
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
