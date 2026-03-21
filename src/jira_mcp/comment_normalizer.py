from __future__ import annotations

from html import unescape
from typing import Any
import re


TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = TAG_RE.sub(" ", value)
    text = unescape(text)
    return WHITESPACE_RE.sub(" ", text).strip()


def truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return f"{value[:limit]}... [truncated]", True


def detect_requirement_signal(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "should",
        "must",
        "need",
        "required",
        "acceptance",
        "expected",
        "design",
        "constraint",
        "edge case",
        "qa",
        "analyst",
    )
    return any(marker in lowered for marker in markers)


def normalize_comment(comment: dict[str, Any], rendered_html: str | None, limit: int) -> dict[str, Any]:
    body_text, truncated = truncate_text(html_to_text(rendered_html or comment.get("body", "")), limit)
    return {
        "id": comment.get("id"),
        "author": {
            "name": (comment.get("author") or {}).get("displayName"),
            "email": (comment.get("author") or {}).get("emailAddress"),
            "active": (comment.get("author") or {}).get("active"),
        },
        "created": comment.get("created"),
        "updated": comment.get("updated"),
        "body_text": body_text,
        "truncated": truncated,
        "contains_requirement_signal": detect_requirement_signal(body_text),
        "visibility": comment.get("visibility"),
    }
