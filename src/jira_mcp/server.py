from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from .jira_api import JiraAdapter, JiraApiError, build_jira_adapter
from .models import IssueForReview, ParsedIssueRef
from .normalizers import normalize_issue_for_review


mcp = FastMCP(
    "Jira Review",
    instructions=(
        "Focused Jira MCP for reviewing and editing issues. Resolve profiles from issue "
        "URLs or configured issue-key prefixes.\n"
        "Read tools (get_issue_for_review, parse_issue_url, list_transitions, my_issues, "
        "find_issues, search_users, list_worklogs, whoami, list_issue_types, "
        "get_create_metadata) do not mutate. Write tools (update_issue, add_comment, "
        "delete_comment, transition_issue, create_issue, log_work, assign_issue, "
        "unassign_issue) change real issues — use only with explicit user intent, and confirm "
        "before destructive or hard-to-reverse actions.\n"
        "Operational notes:\n"
        "- update_issue SETS fields and replaces multi-value fields (labels, components); use "
        "add/remove to change them without clobbering existing values.\n"
        "- Transitions are state- and permission-dependent and often one-way: call "
        "list_transitions first; reverting a status can take several hops.\n"
        "- transition_issue posts its optional comment as a separate comment (reliable), not "
        "embedded in the transition payload.\n"
        "- Sub-tasks cannot nest: create with a sub-task issue type and fields.parent under a "
        "standard (non-sub-task) issue.\n"
        "- Assignee by name, no id needed: resolve a person with search_users, confirm the "
        "right match with the user, then assign_issue with the returned `assignee` value. "
        "Display names are stored in Latin script — a Cyrillic query will not match, so "
        "transliterate (e.g. 'Омуркулов' -> 'Omurkulov'); a first+last name disambiguates.\n"
        "- find_issues lists another person's issues by name (resolved like search_users) plus "
        "broad filters (status, status_category, project, text). If it returns "
        "status='assignee_not_found' or 'assignee_ambiguous', tell the user and ask them to "
        "refine — do not guess who was meant.\n"
        "- log_work adds time (Jira duration like '3h', '1h 30m'); it does NOT go through "
        "update_issue (worklog is not a settable field). list_worklogs reviews logged time.\n"
        "- Before create_issue on an unfamiliar project, use list_issue_types and "
        "get_create_metadata to pick a valid type and satisfy required fields.\n"
        "- delete_comment is irreversible; create_issue has no delete (cancel instead); "
        "notify_users=false needs Jira admin rights."
    ),
    json_response=True,
)


def _translate_error(exc: Exception) -> ValueError:
    return ValueError(str(exc))


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


async def _resolve_issue_context(issue_key_or_url: str) -> tuple[JiraProfile, JiraAdapter, str]:
    if _is_url(issue_key_or_url):
        profile = resolve_profile_for_url(issue_key_or_url)
        _, issue_key = parse_issue_url_parts(issue_key_or_url)
    else:
        issue_key = issue_key_or_url.strip().upper()
        profile = resolve_profile_for_issue_key(issue_key)
    return profile, build_jira_adapter(profile), issue_key


def _resolve_project_context(project_or_prefix: str) -> tuple[JiraProfile, JiraAdapter, str]:
    """Resolve (profile, adapter, project_key) for create/discovery tools keyed by prefix."""
    project_key = project_or_prefix.strip().upper()
    profile = resolve_profile_for_issue_key(project_key)
    return profile, build_jira_adapter(profile), project_key


async def _collect_across_profiles(
    work: Callable[[JiraProfile, JiraAdapter], Awaitable[Any]],
) -> tuple[list[Any], list[dict[str, str]]]:
    """Run `work` on every configured profile in isolation; return (results, per-profile errors)."""
    try:
        profiles = load_jira_profiles()
    except ConfigError as exc:
        raise _translate_error(exc) from exc
    results: list[Any] = []
    errors: list[dict[str, str]] = []
    for profile in profiles:
        try:
            adapter = build_jira_adapter(profile)
        except (ConfigError, JiraApiError) as exc:
            errors.append({"profile": profile.resolved_name, "error": str(exc)})
            continue
        try:
            results.append(await work(profile, adapter))
        except (ConfigError, JiraApiError) as exc:
            errors.append({"profile": profile.resolved_name, "error": str(exc)})
        finally:
            await adapter.aclose()
    return results, errors


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


_ADJUST_ESTIMATE_MODES = ("auto", "leave")


# Friendly status-category synonyms (EN + RU) -> Jira's canonical statusCategory names.
# Unrecognized values pass through unchanged so an exact category name still works.
_STATUS_CATEGORY_ALIASES: dict[str, str] = {
    "to do": "To Do", "todo": "To Do", "new": "To Do", "open": "To Do",
    "к выполнению": "To Do", "к исполнению": "To Do", "открыто": "To Do", "новая": "To Do",
    "in progress": "In Progress", "in-progress": "In Progress", "inprogress": "In Progress",
    "indeterminate": "In Progress", "в процессе": "In Progress", "в работе": "In Progress",
    "прогресс": "In Progress",
    "done": "Done", "complete": "Done", "completed": "Done", "closed": "Done",
    "готово": "Done", "закрыто": "Done", "завершено": "Done",
}

# Whitelisted JQL sort fields (prevents arbitrary text reaching the ORDER BY clause).
_ORDER_FIELDS = ("updated", "created", "priority", "status", "duedate", "key", "assignee")
_DEFAULT_ORDER = "updated DESC"


def _jql_str(value: str) -> str:
    """Quote and escape a value for safe interpolation into a JQL string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _normalize_status_category(value: str) -> str:
    return _STATUS_CATEGORY_ALIASES.get(value.strip().lower(), value.strip())


def _normalize_order_by(order_by: str) -> str:
    """Validate `field [ASC|DESC]` against the field whitelist; fall back to the default."""
    parts = order_by.strip().split()
    if not parts or parts[0].lower() not in _ORDER_FIELDS:
        return _DEFAULT_ORDER
    field = parts[0].lower()
    direction = parts[1].upper() if len(parts) > 1 and parts[1].upper() in ("ASC", "DESC") else "DESC"
    return f"{field} {direction}"


_ISSUE_LIST_FIELDS = ["summary", "status", "issuetype", "priority", "project", "assignee", "updated"]


def _issue_row(profile: JiraProfile, item: dict[str, Any]) -> dict[str, Any]:
    """Shape one search-result issue for the compact list tools (my_issues / find_issues)."""
    f = item.get("fields", {})
    return {
        "key": item.get("key"),
        "summary": f.get("summary"),
        "status": (f.get("status") or {}).get("name"),
        "type": (f.get("issuetype") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "project": (f.get("project") or {}).get("key"),
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "updated": f.get("updated"),
        "url": _browse_url(profile, item.get("key")),
    }


def _user_row(profile: JiraProfile, user: dict[str, Any]) -> dict[str, Any]:
    """Shape one user hit; `assignee` is the value to pass to assign_issue on this profile."""
    return {
        "assignee": user.get("accountId") if profile.deployment == "cloud" else user.get("name"),
        "name": user.get("name"),
        "display_name": user.get("displayName"),
        "email": user.get("emailAddress"),
        "active": user.get("active", True),
        "profile": profile.resolved_name,
    }


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
            await adapter.transition_issue(issue_key, transition_id)
            # Post the comment separately (a comment-less transition screen would drop it).
            # The transition already succeeded, so a comment failure is reported, not raised.
            result: dict[str, Any] = {
                "issue_key": issue_key,
                "status": "transitioned",
                "transition_id": transition_id,
                "commented": False,
                "url": _browse_url(profile, issue_key),
            }
            if comment:
                try:
                    await adapter.add_comment(issue_key, comment)
                    result["commented"] = True
                except (ConfigError, JiraApiError) as exc:
                    result["comment_error"] = str(exc)
            return result
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
        profile, adapter, project_key = _resolve_project_context(project_or_prefix)
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


async def _search_rows_across_profiles(
    jql: str, max_results: int
) -> tuple[list[dict[str, Any]], bool, list[dict[str, str]]]:
    """Run one JQL search on every configured profile and merge the issue rows.

    A single profile keeps the server-side JQL order (its ORDER BY). Only a genuine
    multi-profile merge is re-sorted client-side by `updated` — the one field we always hold —
    so the union is ordered rather than just grouped, instead of overriding the requested order.
    """

    async def _work(profile: JiraProfile, adapter: JiraAdapter) -> dict[str, Any]:
        data = await adapter.search_issues(jql, fields=_ISSUE_LIST_FIELDS, max_results=max_results)
        fetched = data.get("issues", [])
        total = data.get("total", len(fetched))
        return {"rows": [_issue_row(profile, item) for item in fetched], "truncated": total > len(fetched)}

    per_profile, errors = await _collect_across_profiles(_work)
    issues = [row for chunk in per_profile for row in chunk["rows"]]
    if len(per_profile) > 1:
        issues.sort(key=lambda issue: issue.get("updated") or "", reverse=True)
    truncated = any(chunk["truncated"] for chunk in per_profile)
    return issues, truncated, errors


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
        clauses.append(f"project = {_jql_str(project.strip().upper())}")
    jql = " AND ".join(clauses) + " ORDER BY updated DESC"
    issues, truncated, errors = await _search_rows_across_profiles(jql, max_results)
    return {"count": len(issues), "jql": jql, "truncated": truncated, "issues": issues, "errors": errors}


@mcp.tool()
async def whoami(ctx: Context) -> dict[str, Any]:
    """Identify the current user on each configured Jira profile (read-only).

    Useful for assignee values: Data Center uses `name` (the username, often the email),
    Cloud uses `account_id`. Nothing is modified.
    """
    del ctx

    async def _work(profile: JiraProfile, adapter: JiraAdapter) -> dict[str, Any]:
        me = await adapter.get_myself()
        return {
            "profile": profile.resolved_name,
            "name": me.get("name"),
            "account_id": me.get("accountId"),
            "display_name": me.get("displayName"),
            "email": me.get("emailAddress"),
        }

    users, errors = await _collect_across_profiles(_work)
    return {"users": users, "errors": errors}


@mcp.tool()
async def list_issue_types(project_or_prefix: str, ctx: Context) -> dict[str, Any]:
    """List the issue types available for creating issues in a project (read-only).

    Use before create_issue to pick a valid `issue_type` (and see which are sub-tasks).
    """
    del ctx
    try:
        _, adapter, project_key = _resolve_project_context(project_or_prefix)
        try:
            meta = await adapter.get_create_meta(project_key, expand="projects.issuetypes")
        finally:
            await adapter.aclose()
        types = [
            {"id": it.get("id"), "name": it.get("name"), "subtask": it.get("subtask", False)}
            for proj in meta.get("projects", [])
            for it in proj.get("issuetypes", [])
        ]
        return {"project": project_key, "issue_types": types}
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def get_create_metadata(project_or_prefix: str, issue_type: str, ctx: Context) -> dict[str, Any]:
    """List the fields (and which are required) for creating an issue of a given type (read-only).

    Use before create_issue to satisfy mandatory fields. `issue_type` may be its name
    (case-insensitive) or id.
    """
    del ctx
    try:
        wanted = issue_type.strip().lower()
        wanted_id = issue_type.strip()
        _, adapter, project_key = _resolve_project_context(project_or_prefix)
        try:
            meta = await adapter.get_create_meta(project_key, expand="projects.issuetypes.fields")
        finally:
            await adapter.aclose()
        all_fields: list[dict[str, Any]] | None = None
        for proj in meta.get("projects", []):
            for it in proj.get("issuetypes", []):
                if str(it.get("name", "")).strip().lower() == wanted or str(it.get("id")) == wanted_id:
                    all_fields = [
                        {"id": fid, "name": spec.get("name"), "required": spec.get("required", False)}
                        for fid, spec in it.get("fields", {}).items()
                    ]
                    break
            if all_fields is not None:
                break
        if all_fields is None:
            raise ValueError(f"Issue type '{issue_type}' not found for project {project_key}.")
        return {
            "project": project_key,
            "issue_type": issue_type,
            "required_fields": [f for f in all_fields if f["required"]],
            "all_fields": all_fields,
        }
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def log_work(
    issue_key_or_url: str,
    time_spent: str,
    ctx: Context,
    comment: str | None = None,
    started: str | None = None,
    adjust_estimate: str = "auto",
) -> dict[str, Any]:
    """Log time worked on a Jira issue (adds a worklog entry).

    `time_spent` uses Jira duration syntax, e.g. "3h", "1h 30m", "2d 4h". `started` is an
    optional ISO-8601 timestamp Jira accepts (yyyy-MM-dd'T'HH:mm:ss.SSS+ZZZZ); omit to let
    Jira default it to now. `adjust_estimate` controls the remaining estimate: "auto"
    (subtract the logged time, default) or "leave" (keep it unchanged). This does NOT go
    through update_issue — worklog is not a settable field, it has its own endpoint.
    """
    del ctx
    if not time_spent or not time_spent.strip():
        raise ValueError("log_work needs a non-empty time_spent, e.g. '3h' or '1h 30m'.")
    if adjust_estimate not in _ADJUST_ESTIMATE_MODES:
        raise ValueError(
            f"adjust_estimate must be one of {_ADJUST_ESTIMATE_MODES}; got '{adjust_estimate}'."
        )
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            created = await adapter.add_worklog(
                issue_key,
                time_spent=time_spent.strip(),
                comment=comment,
                started=started,
                adjust_estimate=adjust_estimate,
            )
            return {
                "issue_key": issue_key,
                "status": "worklog_added",
                "worklog_id": created.get("id"),
                "time_spent": created.get("timeSpent"),
                "time_spent_seconds": created.get("timeSpentSeconds"),
                "started": created.get("started"),
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def list_worklogs(issue_key_or_url: str, ctx: Context) -> dict[str, Any]:
    """List the worklog entries (logged time) on a Jira issue (read-only).

    Returns each entry's author, time spent, start time, and comment, plus the total logged
    seconds. Use to review or verify time logged via log_work. Nothing is modified.
    """
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            data = await adapter.get_worklogs(issue_key)
        finally:
            await adapter.aclose()
        worklogs = [
            {
                "id": w.get("id"),
                "author": (w.get("author") or {}).get("displayName"),
                "time_spent": w.get("timeSpent"),
                "time_spent_seconds": w.get("timeSpentSeconds"),
                "started": w.get("started"),
                "comment": w.get("comment"),
            }
            for w in data.get("worklogs", [])
        ]
        total_seconds = sum(w["time_spent_seconds"] or 0 for w in worklogs)
        return {
            "issue_key": issue_key,
            "count": len(worklogs),
            "total_time_spent_seconds": total_seconds,
            "worklogs": worklogs,
            "url": _browse_url(profile, issue_key),
        }
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def search_users(query: str, ctx: Context, max_results: int = 20) -> dict[str, Any]:
    """Find Jira users by name, username, or email across configured profiles (read-only).

    Resolves a person to their assignee identifier so the user need not know the exact
    username/id. Each result's `assignee` field is the value to pass to assign_issue. Display
    names are stored in Latin script, so a Cyrillic query will not match — transliterate (e.g.
    'Омуркулов' -> 'Omurkulov'); a first+last name disambiguates common surnames. Confirm the
    right person with the user before assigning. Nothing is modified.
    """
    del ctx
    if not query or not query.strip():
        raise ValueError("search_users needs a non-empty query (name, username, or email).")

    async def _work(profile: JiraProfile, adapter: JiraAdapter) -> list[dict[str, Any]]:
        hits = await adapter.search_users(query.strip(), max_results=max_results)
        return [_user_row(profile, user) for user in hits]

    per_profile, errors = await _collect_across_profiles(_work)
    users = [row for chunk in per_profile for row in chunk]
    return {"query": query.strip(), "count": len(users), "users": users, "errors": errors}


@mcp.tool()
async def assign_issue(issue_key_or_url: str, assignee: str, ctx: Context) -> dict[str, Any]:
    """Assign a Jira issue to a user via the dedicated assignee endpoint.

    `assignee` is the exact identifier from search_users — the username/`name` on Data Center
    (often the email), or the accountId on Cloud. Resolve the person with search_users and
    confirm the match with the user before assigning. To clear the assignee use unassign_issue.
    """
    del ctx
    if not assignee or not assignee.strip():
        raise ValueError("assign_issue needs a non-empty assignee; use unassign_issue to clear.")
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            await adapter.assign_issue(issue_key, assignee.strip())
            return {
                "issue_key": issue_key,
                "status": "assigned",
                "assignee": assignee.strip(),
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def unassign_issue(issue_key_or_url: str, ctx: Context) -> dict[str, Any]:
    """Clear the assignee of a Jira issue (leave it unassigned).

    Uses the dedicated assignee endpoint. Confirm intent before calling.
    """
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            await adapter.assign_issue(issue_key, None)
            return {
                "issue_key": issue_key,
                "status": "unassigned",
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


_ASSIGNEE_NOT_FOUND_HINT = (
    "No Jira user matched the query. Display names are stored in Latin script, so a Cyrillic "
    "query will not match — try Latin (surname, or first+last name) or the person's email. "
    "Tell the user no match was found and ask them to refine; do not guess."
)
_ASSIGNEE_AMBIGUOUS_HINT = (
    "Multiple users matched the query. Do not guess — show the candidates to the user and ask "
    "which one, or refine with first+last name / email / exact username, then retry."
)


def _filter_clauses(
    status: str | list[str] | None,
    status_category: str | None,
    project: str | None,
    text: str | None,
    only_open: bool,
) -> list[str]:
    """Build the non-assignee JQL clauses shared by the assignee and broad find_issues paths."""
    clauses: list[str] = []
    if only_open:
        clauses.append("statusCategory != Done")
    if status_category:
        clauses.append(f"statusCategory = {_jql_str(_normalize_status_category(status_category))}")
    if status:
        values = status if isinstance(status, list) else [status]
        rendered = ", ".join(_jql_str(str(value)) for value in values)
        clauses.append(f"status in ({rendered})")
    if project:
        clauses.append(f"project = {_jql_str(project.strip().upper())}")
    if text:
        clauses.append(f"text ~ {_jql_str(text)}")
    return clauses


def _assemble_jql(clauses: list[str], order: str) -> str:
    prefix = f"{' AND '.join(clauses)} " if clauses else ""
    return f"{prefix}ORDER BY {order}".strip()


@mcp.tool()
async def find_issues(
    ctx: Context,
    assignee: str | None = None,
    status: str | list[str] | None = None,
    status_category: str | None = None,
    project: str | None = None,
    text: str | None = None,
    only_open: bool = False,
    order_by: str = _DEFAULT_ORDER,
    max_results: int = 50,
) -> dict[str, Any]:
    """Search Jira issues with flexible filters, incl. by another person's name (read-only).

    `assignee` is a person query (name / email / username) resolved to a user — no id needed;
    e.g. find_issues(assignee="Aidin Omurkulov", status_category="in progress"). If it matches
    exactly one user, their issues are returned; if none, status='assignee_not_found'; if
    several, status='assignee_ambiguous' with candidates and NO issues — surface that to the
    user and let them pick instead of guessing.

    Filters (all optional, combined with AND): `status` (exact status name or list),
    `status_category` ("to do" / "in progress" / "done", EN or RU synonyms), `project` (key),
    `text` (free-text match), `only_open` (exclude Done), `order_by` (e.g. "updated DESC").
    Without `assignee`, searches across all configured profiles. Nothing is modified.
    """
    del ctx
    order = _normalize_order_by(order_by)
    base_clauses = _filter_clauses(status, status_category, project, text, only_open)

    if assignee is not None:
        query = assignee.strip()
        if not query:
            raise ValueError("assignee query must not be empty.")

        async def _resolve(profile: JiraProfile, adapter: JiraAdapter) -> list[tuple[JiraProfile, dict[str, Any]]]:
            hits = await adapter.search_users(query, max_results=max(max_results, 20))
            return [(profile, user) for user in hits]

        per_profile, resolve_errors = await _collect_across_profiles(_resolve)
        matches = [pair for chunk in per_profile for pair in chunk]

        if not matches:
            return {
                "status": "assignee_not_found",
                "assignee_query": query,
                "candidates": [],
                "count": 0,
                "issues": [],
                "hint": _ASSIGNEE_NOT_FOUND_HINT,
                "errors": resolve_errors,
            }
        if len(matches) > 1:
            return {
                "status": "assignee_ambiguous",
                "assignee_query": query,
                "candidates": [_user_row(profile, user) for profile, user in matches][:20],
                "count": 0,
                "issues": [],
                "hint": _ASSIGNEE_AMBIGUOUS_HINT,
                "errors": resolve_errors,
            }

        matched_profile, matched_user = matches[0]
        matched_row = _user_row(matched_profile, matched_user)
        jql = _assemble_jql(
            [*base_clauses, f"assignee = {_jql_str(matched_row['assignee'] or '')}"], order
        )
        try:
            adapter = build_jira_adapter(matched_profile)
            try:
                data = await adapter.search_issues(
                    jql, fields=_ISSUE_LIST_FIELDS, max_results=max_results
                )
            finally:
                await adapter.aclose()
        except (ConfigError, JiraApiError) as exc:
            raise _translate_error(exc) from exc
        fetched = data.get("issues", [])
        total = data.get("total", len(fetched))
        return {
            "status": "ok",
            "matched_user": matched_row,
            "jql": jql,
            "count": len(fetched),
            "truncated": total > len(fetched),
            "issues": [_issue_row(matched_profile, item) for item in fetched],
            "errors": resolve_errors,
        }

    jql = _assemble_jql(base_clauses, order)
    issues, truncated, errors = await _search_rows_across_profiles(jql, max_results)
    return {
        "status": "ok",
        "jql": jql,
        "count": len(issues),
        "truncated": truncated,
        "issues": issues,
        "errors": errors,
    }


def main() -> None:
    mcp.run()
