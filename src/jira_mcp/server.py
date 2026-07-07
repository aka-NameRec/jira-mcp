from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .config import (
    ConfigError,
    JiraProfile,
    load_jira_profiles,
    resolve_profile_for_issue_key,
    resolve_profile_for_url,
)
from .field_mapping import FieldMapping, build_field_mapping
from .issue_parser import parse_issue_url as parse_issue_url_parts
from .jira_api import JiraApiError, build_jira_adapter
from .models import IssueForReview, ParsedIssueRef
from .normalizers import normalize_issue_for_review


mcp = FastMCP(
    "Jira Review",
    instructions=(
        "Focused Jira MCP server for requirement review and editing workflows. "
        "Resolve Jira profiles from issue URLs or configured issue key prefixes. "
        "Write tools (update_issue, add_comment, delete_comment, transition_issue, "
        "create_issue) modify real Jira issues; use them only with explicit user intent. "
        "delete_comment is destructive and irreversible."
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


_SEMANTIC_FIELDS = ("acceptance_criteria", "business_context", "design_links")


def _translate_fields(fields: dict[str, Any], mapping: FieldMapping) -> dict[str, Any]:
    """Map semantic field aliases to the profile's customfield ids; pass other keys through."""
    translated: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _SEMANTIC_FIELDS:
            custom_id = getattr(mapping, key)
            if not custom_id:
                raise ValueError(
                    f"Field alias '{key}' has no customfield mapping configured for this profile."
                )
            translated[custom_id] = value
        else:
            translated[key] = value
    return translated


def _resolve_transition_id(transition: str, available: list[dict[str, Any]]) -> str:
    """Resolve a transition id from an id or a case-insensitive transition name."""
    wanted = transition.strip()
    for item in available:
        if str(item.get("id")) == wanted:
            return str(item["id"])
    lowered = wanted.lower()
    for item in available:
        if str(item.get("name", "")).strip().lower() == lowered:
            return str(item["id"])
    names = ", ".join(f"{item.get('name')} (id={item.get('id')})" for item in available) or "none"
    raise ValueError(f"No transition matching '{transition}'. Available: {names}.")


def _as_list(values: Any) -> list[Any]:
    """A single value or a list -> list; guards against iterating a string char-by-char."""
    return values if isinstance(values, list) else [values]


def _build_update_ops(
    add: dict[str, Any] | None, remove: dict[str, Any] | None
) -> dict[str, list[dict[str, Any]]]:
    """Translate add/remove field maps into Jira `update` verb operations (no clobber)."""
    ops: dict[str, list[dict[str, Any]]] = {}
    for field, values in (add or {}).items():
        ops.setdefault(field, []).extend({"add": value} for value in _as_list(values))
    for field, values in (remove or {}).items():
        ops.setdefault(field, []).extend({"remove": value} for value in _as_list(values))
    return ops


def _browse_url(profile: JiraProfile, issue_key: str | None) -> str:
    return f"{profile.normalized_base_url}/browse/{issue_key}"


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


@mcp.tool()
async def update_issue(
    issue_key_or_url: str,
    ctx: Context,
    fields: dict[str, Any] | None = None,
    add: dict[str, Any] | None = None,
    remove: dict[str, Any] | None = None,
    notify_users: bool = True,
) -> dict[str, Any]:
    """Update a Jira issue.

    `fields` SETS values and REPLACES multi-value fields — e.g. {"summary": "..."} or
    {"description": "..."}; semantic aliases 'acceptance_criteria' / 'business_context' /
    'design_links' map to the profile's customfield ids.

    To change multi-value fields (labels, components, ...) WITHOUT clobbering existing
    values, use `add` / `remove`: a mapping of field id -> list of values applied via Jira's
    update verb, e.g. add={"labels": ["china"]}, remove={"labels": ["old"]}.
    """
    del ctx
    if not fields and not add and not remove:
        raise ValueError("update_issue needs at least one of: fields, add, remove.")
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            translated = _translate_fields(fields, build_field_mapping(profile)) if fields else None
            update_ops = _build_update_ops(add, remove)
            await adapter.update_issue(
                issue_key, translated, update=update_ops or None, notify_users=notify_users
            )
            changed = sorted(set((translated or {}).keys()) | set(update_ops.keys()))
            return {
                "issue_key": issue_key,
                "status": "updated",
                "updated_fields": changed,
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def add_comment(issue_key_or_url: str, body: str, ctx: Context) -> dict[str, Any]:
    """Add a comment to an existing Jira issue."""
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            created = await adapter.add_comment(issue_key, body)
            return {
                "issue_key": issue_key,
                "status": "commented",
                "comment_id": created.get("id"),
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def delete_comment(issue_key_or_url: str, comment_id: str, ctx: Context) -> dict[str, Any]:
    """Delete a comment from a Jira issue. DESTRUCTIVE and irreversible.

    Pass the comment id (e.g. from get_issue_for_review). Only the given comment is removed;
    the issue itself is untouched. Confirm intent before calling.
    """
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            await adapter.delete_comment(issue_key, str(comment_id))
            return {
                "issue_key": issue_key,
                "status": "comment_deleted",
                "comment_id": str(comment_id),
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def transition_issue(
    issue_key_or_url: str,
    transition: str,
    ctx: Context,
    comment: str | None = None,
) -> dict[str, Any]:
    """Move a Jira issue through a workflow transition.

    `transition` may be the transition id or its case-insensitive name (e.g. "In Progress").
    Only transitions currently available for the issue are accepted. Optionally attach a
    comment posted together with the transition.
    """
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            available = (await adapter.get_transitions(issue_key)).get("transitions", [])
            transition_id = _resolve_transition_id(transition, available)
            await adapter.transition_issue(issue_key, transition_id, comment=comment)
            return {
                "issue_key": issue_key,
                "status": "transitioned",
                "transition_id": transition_id,
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def create_issue(
    project_or_prefix: str,
    issue_type: str,
    summary: str,
    ctx: Context,
    description: str | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new Jira issue.

    `project_or_prefix` is the target Jira project key (e.g. "BL"); it also selects the
    profile by matching a configured issue-key prefix. `fields` may carry extra Jira fields
    or the semantic aliases translated to customfield ids.
    """
    del ctx
    try:
        project_key = project_or_prefix.strip().upper()
        profile = resolve_profile_for_issue_key(project_key)
        adapter = build_jira_adapter(profile)
        try:
            payload: dict[str, Any] = {
                "project": {"key": project_key},
                "issuetype": {"name": issue_type},
                "summary": summary,
            }
            if description is not None:
                payload["description"] = description
            if fields:
                payload.update(_translate_fields(fields, build_field_mapping(profile)))
            created = await adapter.create_issue(payload)
            key = created.get("key")
            return {
                "issue_key": key,
                "status": "created",
                "url": _browse_url(profile, key) if key else None,
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def list_transitions(issue_key_or_url: str, ctx: Context) -> dict[str, Any]:
    """List the workflow transitions available for a Jira issue right now (read-only).

    Transitions are state- and permission-dependent, so this returns only the moves valid
    from the issue's current status. Use a returned id or name as the `transition` argument
    of transition_issue. Nothing is modified.
    """
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            data = await adapter.get_transitions(issue_key)
            transitions = [
                {
                    "id": str(item.get("id")),
                    "name": item.get("name"),
                    "to_status": (item.get("to") or {}).get("name"),
                }
                for item in data.get("transitions", [])
            ]
            return {
                "issue_key": issue_key,
                "transitions": transitions,
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


_MY_ISSUES_FIELDS = ["summary", "status", "issuetype", "priority", "project", "updated"]


@mcp.tool()
async def my_issues(
    ctx: Context,
    only_open: bool = True,
    project: str | None = None,
    max_results: int = 50,
) -> dict[str, Any]:
    """List Jira issues assigned to the current user, across every configured project (read-only).

    Runs `assignee = currentUser()` on each configured Jira profile and merges the results.
    Set only_open=False to include done/closed issues, or pass a project key (e.g. "BL",
    "MKT", "DEVOPS") to narrow to one project. Nothing is modified.
    """
    del ctx
    clauses = ["assignee = currentUser()"]
    if only_open:
        clauses.append("statusCategory != Done")
    if project:
        clauses.append(f"project = {project.strip().upper()}")
    jql = " AND ".join(clauses) + " ORDER BY updated DESC"

    try:
        profiles = load_jira_profiles()
    except ConfigError as exc:
        raise _translate_error(exc) from exc

    issues: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for profile in profiles:
        try:
            adapter = build_jira_adapter(profile)
        except (ConfigError, JiraApiError) as exc:
            errors.append({"profile": profile.resolved_name, "error": str(exc)})
            continue
        try:
            data = await adapter.search_issues(jql, fields=_MY_ISSUES_FIELDS, max_results=max_results)
        except (ConfigError, JiraApiError) as exc:
            errors.append({"profile": profile.resolved_name, "error": str(exc)})
            data = None
        finally:
            await adapter.aclose()
        for item in (data or {}).get("issues", []):
            fields = item.get("fields", {})
            issues.append(
                {
                    "key": item.get("key"),
                    "summary": fields.get("summary"),
                    "status": (fields.get("status") or {}).get("name"),
                    "type": (fields.get("issuetype") or {}).get("name"),
                    "priority": (fields.get("priority") or {}).get("name"),
                    "project": (fields.get("project") or {}).get("key"),
                    "updated": fields.get("updated"),
                    "url": _browse_url(profile, item.get("key")),
                }
            )
    # Global sort so the merge across profiles is truly ordered, not just grouped.
    issues.sort(key=lambda issue: issue.get("updated") or "", reverse=True)
    return {"count": len(issues), "jql": jql, "issues": issues, "errors": errors}


def main() -> None:
    mcp.run()
