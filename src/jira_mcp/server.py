from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .config import (
    ConfigError,
    JiraProfile,
    resolve_profile_for_issue_key,
    resolve_profile_for_url,
)
from .field_mapping import build_field_mapping
from .issue_parser import parse_issue_url as parse_issue_url_parts
from .jira_api import JiraApiError, build_jira_adapter
from .models import IssueForReview, ParsedIssueRef
from .normalizers import normalize_issue_for_review


mcp = FastMCP(
    "Jira Review",
    instructions=(
        "Focused Jira MCP server for requirement review workflows. "
        "Resolve Jira profiles from issue URLs or configured issue key prefixes."
    ),
    json_response=True,
)


def _translate_error(exc: Exception) -> ValueError:
    return ValueError(str(exc))


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


async def _resolve_issue_context(issue_key_or_url: str) -> tuple[JiraProfile, Any, str]:
    if _is_url(issue_key_or_url):
        profile = resolve_profile_for_url(issue_key_or_url)
        _, issue_key = parse_issue_url_parts(issue_key_or_url)
    else:
        issue_key = issue_key_or_url.strip().upper()
        profile = resolve_profile_for_issue_key(issue_key)
    client = build_jira_adapter(profile)
    return profile, client, issue_key


@mcp.tool()
async def parse_issue_url(url: str, ctx: Context) -> dict[str, Any]:
    """Parse a Jira issue URL into a stable issue reference."""
    del ctx
    try:
        profile = resolve_profile_for_url(url)
        _, issue_key = parse_issue_url_parts(url)
        adapter = build_jira_adapter(profile)
        try:
            parsed = {
                "url": url,
                "host": profile.host,
                "profile_name": profile.resolved_name,
                "base_url": profile.normalized_base_url,
                "issue_key": issue_key,
                "api_url": adapter.build_api_issue_url(issue_key),
            }
            return ParsedIssueRef.model_validate(parsed).model_dump(mode="json")
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def get_issue_for_review(issue_key_or_url: str, ctx: Context) -> dict[str, Any]:
    """Fetch issue data, comments, and attachment metadata assembled for review workflows."""
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            raw_issue = await adapter.get_issue(issue_key)
            normalized = normalize_issue_for_review(
                raw_issue,
                profile,
                build_field_mapping(profile),
                adapter,
            )
            return IssueForReview.model_validate(normalized).model_dump(mode="json")
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


def main() -> None:
    mcp.run()
