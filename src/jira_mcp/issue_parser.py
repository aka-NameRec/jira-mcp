from __future__ import annotations

from urllib.parse import urlsplit

from .config import ConfigError


def parse_issue_url(issue_url: str) -> tuple[str, str]:
    parsed = urlsplit(issue_url)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError("Issue URL must be http or https.")
    parts = [part for part in parsed.path.split("/") if part]
    try:
        browse_index = parts.index("browse")
        issue_key = parts[browse_index + 1]
    except (ValueError, IndexError) as exc:
        raise ConfigError("URL does not look like a Jira issue browse URL.") from exc
    if not issue_key:
        raise ConfigError("Unable to determine issue key from URL.")
    return parsed.netloc, issue_key.upper()
