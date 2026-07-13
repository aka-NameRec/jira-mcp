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
        "get_create_metadata, list_link_types, list_priorities, find_sprints) do not mutate. "
        "Write tools (update_issue, add_comment, delete_comment, transition_issue, "
        "create_issue, log_work, assign_issue, unassign_issue, link_issues, set_sprint, "
        "unlink_issues, remove_from_sprint, delete_worklog) change real issues — use only with "
        "explicit user intent, and confirm before destructive or hard-to-reverse actions. Each "
        "of link_issues / set_sprint / log_work has an inverse for cleanup: unlink_issues / "
        "remove_from_sprint / delete_worklog.\n"
        "Common recipes (compose small tools; there is no mega-tool):\n"
        "- Create a task for a teammate, in a sprint, with priority and estimate: search_users "
        "-> confirm the person -> create_issue(fields={'priority': {'name': 'High'}, "
        "'timetracking': {'originalEstimate': '2h'}}) -> set_sprint(new_key, <id from "
        "find_sprints>). Use list_priorities / find_sprints first to get exact values.\n"
        "- Make issue A block issue B: link_issues(outward_issue='A', inward_issue='B', "
        "link_type='Blocks'); list_link_types shows valid names/phrases.\n"
        "- Set the deadline vs the estimate: a due date is fields={'duedate': 'YYYY-MM-DD'}; a "
        "bare duration like '2h' is an estimate (timetracking.originalEstimate); time already "
        "spent is log_work — these are three different things.\n"
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
        "- link_issues links two issues on the SAME instance; the sentence is 'outward_issue "
        "<type> inward_issue'. Pass a link_type name or a directional phrase — naming the "
        "inward phrase swaps the issues automatically. See list_link_types for valid values.\n"
        "- find_sprints needs Jira Software scrum boards; if it returns several sprints, ask the "
        "user which one and never guess. set_sprint takes the numeric sprint id it returned. "
        "Sub-tasks follow their parent's sprint — set_sprint on a sub-task reports success but "
        "does not move it; sprint the parent instead.\n"
        "- Priority names are instance-specific — call list_priorities before setting one.\n"
        "- unlink_issues removes the link(s) between two issues; if several link types connect "
        "them it returns status='ambiguous_link_type' and asks for link_type — don't guess. "
        "delete_worklog takes a worklog_id from list_worklogs.\n"
        "- delete_comment is irreversible; create_issue has no delete (cancel via transition "
        "instead); notify_users=false needs Jira admin rights."
    ),
    json_response=True,
)


def _translate_error(exc: Exception) -> ValueError:
    return ValueError(str(exc))


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _resolve_issue_profile(issue_key_or_url: str) -> tuple[JiraProfile, str]:
    """Resolve (profile, issue_key) from an issue key or URL, without building an adapter."""
    if _is_url(issue_key_or_url):
        profile = resolve_profile_for_url(issue_key_or_url)
        _, issue_key = parse_issue_url_parts(issue_key_or_url)
    else:
        issue_key = issue_key_or_url.strip().upper()
        profile = resolve_profile_for_issue_key(issue_key)
    return profile, issue_key


async def _resolve_issue_context(issue_key_or_url: str) -> tuple[JiraProfile, JiraAdapter, str]:
    profile, issue_key = _resolve_issue_profile(issue_key_or_url)
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


def _resolve_link_type(link_type: str, available: list[dict[str, Any]]) -> tuple[str, bool]:
    """Resolve a link type name + orientation from a type name or a directional phrase.

    Matches (case-insensitively) the type name, then its outward phrase, then its inward
    phrase. Returns (canonical_name, swap): swap=True means the caller named the INWARD phrase,
    so the outward/inward issues must be swapped for the relationship sentence to hold.
    """
    wanted = link_type.strip().lower()
    for item in available:
        if str(item.get("name", "")).strip().lower() == wanted:
            return str(item.get("name")), False
    for item in available:
        if str(item.get("outward", "")).strip().lower() == wanted:
            return str(item.get("name")), False
    for item in available:
        if str(item.get("inward", "")).strip().lower() == wanted:
            return str(item.get("name")), True
    options = (
        "; ".join(
            f"{item.get('name')} (outward: '{item.get('outward')}', inward: '{item.get('inward')}')"
            for item in available
        )
        or "none"
    )
    raise ValueError(f"No link type matching '{link_type}'. Available: {options}.")


_SPRINT_STATES = ("active", "future", "closed")


def _normalize_sprint_state(state: str) -> str:
    """Validate a comma list of sprint states (or 'all') against the known Agile states."""
    raw = state.strip().lower()
    if raw == "all":
        return ",".join(_SPRINT_STATES)
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    invalid = [token for token in tokens if token not in _SPRINT_STATES]
    if not tokens or invalid:
        raise ValueError(
            f"state must be a comma list of {_SPRINT_STATES} or 'all'; got '{state}'."
        )
    return ",".join(tokens)


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

    Common extra `fields`:
    - priority: {"priority": {"name": "High"}} — names are instance-specific, so call
      list_priorities first (this instance has both "High" and "Highest").
    - original estimate (planned time to complete): {"timetracking": {"originalEstimate": "2h"}}
      — a bare duration like "2h"/"1d" is an ESTIMATE, NOT a deadline.
    - due date (a calendar deadline): {"duedate": "YYYY-MM-DD"}.
    Time ALREADY spent is not set here — use log_work. If the user just says "set 2h", ask
    whether that means the estimate or a due date before choosing.
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

    Useful `fields`: assignee {"assignee": {"name": "user@devcats.kg"}} (or assign_issue after
    create); priority {"priority": {"name": "High"}} (see list_priorities); an original
    estimate {"timetracking": {"originalEstimate": "2h"}} (a bare duration is an ESTIMATE, not
    a deadline); a due date {"duedate": "YYYY-MM-DD"}; a sub-task parent {"parent": {"key":
    "BL-100"}}. To put the new issue in a sprint, call set_sprint with the returned key.
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


@mcp.tool()
async def list_link_types(ctx: Context) -> dict[str, Any]:
    """List the issue link types available on each configured Jira instance (read-only).

    Use before link_issues to pick a valid `link_type`. Each entry gives the type `name`
    (e.g. "Blocks", "Relates", "Duplicate") and its directional phrases: `outward` (how the
    outward issue relates to the inward one, e.g. "blocks") and `inward` (the reverse, e.g.
    "is blocked by"). Pass the name OR either phrase to link_issues. Nothing is modified.
    """
    del ctx

    async def _work(profile: JiraProfile, adapter: JiraAdapter) -> list[dict[str, Any]]:
        data = await adapter.get_link_types()
        return [
            {
                "name": item.get("name"),
                "outward": item.get("outward"),
                "inward": item.get("inward"),
                "profile": profile.resolved_name,
            }
            for item in data.get("issueLinkTypes", [])
        ]

    per_profile, errors = await _collect_across_profiles(_work)
    link_types = [row for chunk in per_profile for row in chunk]
    return {"count": len(link_types), "link_types": link_types, "errors": errors}


@mcp.tool()
async def link_issues(
    outward_issue: str,
    inward_issue: str,
    link_type: str,
    ctx: Context,
    comment: str | None = None,
) -> dict[str, Any]:
    """Create a directed link between two Jira issues.

    The relationship reads "`outward_issue` <link_type> `inward_issue`" — e.g.
    link_issues("BL-1", "BL-2", "Blocks") means BL-1 blocks BL-2. `link_type` may be a type
    name (see list_link_types) or a directional phrase; if you pass an INWARD phrase (e.g.
    "is blocked by"), the two issues are swapped automatically so the sentence still holds.
    Both issues must live on the same Jira instance. Optionally attach a `comment`.
    """
    del ctx
    try:
        out_profile, out_key = _resolve_issue_profile(outward_issue)
        in_profile, in_key = _resolve_issue_profile(inward_issue)
        if out_profile.resolved_name != in_profile.resolved_name:
            raise ValueError(
                "Both issues must be on the same Jira instance to link them: "
                f"{out_key} -> {out_profile.resolved_name}, "
                f"{in_key} -> {in_profile.resolved_name}."
            )
        adapter = build_jira_adapter(out_profile)
        try:
            available = (await adapter.get_link_types()).get("issueLinkTypes", [])
            name, swap = _resolve_link_type(link_type, available)
            api_outward, api_inward = (in_key, out_key) if swap else (out_key, in_key)
            await adapter.create_issue_link(
                name, inward_issue=api_inward, outward_issue=api_outward, comment=comment
            )
            outward_phrase = next(
                (item.get("outward") for item in available if str(item.get("name")) == name), None
            )
            return {
                "status": "linked",
                "link_type": name,
                "outward_issue": api_outward,
                "inward_issue": api_inward,
                "relationship": (
                    f"{api_outward} {outward_phrase} {api_inward}" if outward_phrase else name
                ),
                "url": _browse_url(out_profile, api_outward),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def list_priorities(ctx: Context) -> dict[str, Any]:
    """List the priorities available on each configured Jira instance (read-only).

    Priority names are instance-specific — this instance has both "High" and "Highest" plus
    custom ones — so check here before setting one. Set a priority via create_issue/update_issue
    with fields={"priority": {"name": "High"}}. Nothing is modified.
    """
    del ctx

    async def _work(profile: JiraProfile, adapter: JiraAdapter) -> list[dict[str, Any]]:
        data = await adapter.get_priorities()
        return [
            {"id": item.get("id"), "name": item.get("name"), "profile": profile.resolved_name}
            for item in data
        ]

    per_profile, errors = await _collect_across_profiles(_work)
    priorities = [row for chunk in per_profile for row in chunk]
    return {"count": len(priorities), "priorities": priorities, "errors": errors}


_NO_SCRUM_BOARD_HINT = (
    "No scrum board found for this project, so it has no sprints. Only scrum boards have "
    "sprints — kanban boards do not, and a project without Jira Software has none."
)
_NO_SCRUM_BOARD_TRUNCATED_HINT = (
    "No scrum board in the first page of boards, but the board list was truncated — a scrum "
    "board may exist beyond the page. Narrow the project or check the boards in the Jira UI."
)
_NO_SPRINTS_HINT = (
    "No sprints matched the requested state. Try state='all' to include closed sprints, or the "
    "project may have no sprints yet."
)
_SPRINT_PICK_HINT = (
    "Multiple sprints matched. Do not guess — show the candidates to the user and ask which "
    "sprint, then pass its id to set_sprint."
)


@mcp.tool()
async def find_sprints(
    project_or_prefix: str,
    ctx: Context,
    state: str = "active,future",
) -> dict[str, Any]:
    """Find the sprints of a project's scrum boards (read-only; needs Jira Software).

    `state` is a comma list of active/future/closed (or "all"); default "active,future".
    Returns each sprint's id, name, state, board, and start/end. A project can have several
    scrum boards and parallel sprints — if more than one matches what the user wants, ASK which
    one (the result carries a hint); never guess. Pass a returned sprint id to set_sprint.
    status="no_board" means the project has no scrum board; "no_sprints" means none matched.
    Nothing is modified.
    """
    del ctx
    try:
        normalized_state = _normalize_sprint_state(state)
        profile, adapter, project_key = _resolve_project_context(project_or_prefix)
        try:
            boards_data = await adapter.get_boards(project_key)
            boards = boards_data.get("values", [])
            # The board list is paged; flag if a scrum board could sit beyond the fetched page.
            boards_truncated = not boards_data.get("isLast", True)
            board_summaries = [
                {"id": board.get("id"), "name": board.get("name"), "type": board.get("type")}
                for board in boards
            ]
            scrum_boards = [b for b in boards if str(b.get("type", "")).lower() == "scrum"]
            if not scrum_boards:
                return {
                    "status": "no_board",
                    "project": project_key,
                    "boards": board_summaries,
                    "boards_truncated": boards_truncated,
                    "sprints": [],
                    "hint": (
                        _NO_SCRUM_BOARD_TRUNCATED_HINT if boards_truncated else _NO_SCRUM_BOARD_HINT
                    ),
                }
            seen: set[Any] = set()
            sprints: list[dict[str, Any]] = []
            board_errors: list[dict[str, str]] = []
            truncated = False
            for board in scrum_boards:
                board_id = board.get("id")
                try:
                    data = await adapter.get_board_sprints(board_id, state=normalized_state)
                except JiraApiError as exc:
                    board_errors.append(
                        {"board": str(board.get("name")), "error": str(exc)}
                    )
                    continue
                if not data.get("isLast", True):
                    truncated = True
                for sprint in data.get("values", []):
                    sprint_id = sprint.get("id")
                    if sprint_id in seen:
                        continue
                    seen.add(sprint_id)
                    sprints.append(
                        {
                            "id": sprint_id,
                            "name": sprint.get("name"),
                            "state": sprint.get("state"),
                            "board_id": board_id,
                            "board_name": board.get("name"),
                            "start": sprint.get("startDate"),
                            "end": sprint.get("endDate"),
                        }
                    )
        finally:
            await adapter.aclose()
        if not sprints:
            return {
                "status": "no_sprints",
                "project": project_key,
                "state": normalized_state,
                "boards": board_summaries,
                "boards_truncated": boards_truncated,
                "sprints": [],
                "errors": board_errors,
                "hint": _NO_SPRINTS_HINT,
            }
        return {
            "status": "ok",
            "project": project_key,
            "state": normalized_state,
            "boards": board_summaries,
            "boards_truncated": boards_truncated,
            "count": len(sprints),
            "truncated": truncated,
            "sprints": sprints,
            "errors": board_errors,
            "hint": _SPRINT_PICK_HINT if len(sprints) > 1 else None,
        }
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def set_sprint(issue_key_or_url: str, sprint: str | int, ctx: Context) -> dict[str, Any]:
    """Move a Jira issue into a sprint (needs Jira Software).

    `sprint` is the numeric sprint id — get it from find_sprints (a sprint name is not
    accepted, ids are unambiguous). Uses the Agile endpoint, so no Sprint field id is needed.

    Sub-tasks inherit their parent's sprint: the endpoint accepts a sub-task request (returns
    success) but does NOT actually move it. To sprint a sub-task, set the sprint on its parent
    (a standard issue) instead.
    """
    del ctx
    sprint_id = str(sprint).strip()
    if not sprint_id.isdigit():
        raise ValueError(
            "set_sprint needs a numeric sprint id, not a name — get it from find_sprints."
        )
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            await adapter.add_issue_to_sprint(sprint_id, issue_key)
            return {
                "issue_key": issue_key,
                "status": "sprint_set",
                "sprint_id": sprint_id,
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def unlink_issues(
    issue_key_or_url: str,
    related_issue: str,
    ctx: Context,
    link_type: str | None = None,
) -> dict[str, Any]:
    """Remove the link(s) between two issues (the undo of link_issues).

    Reads `issue_key_or_url`'s links, finds the one(s) connecting it to `related_issue`, and
    deletes them. If several link types connect the pair, pass `link_type` (a type name, e.g.
    "Blocks") to choose which; without it the tool refuses and lists the types rather than
    guessing. Find the pair with get_issue_for_review. Re-create a link with link_issues.
    """
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            if _is_url(related_issue):
                _, related_key = parse_issue_url_parts(related_issue)
            else:
                related_key = related_issue.strip().upper()
            issue = await adapter.get_issue(issue_key)
            links = (issue.get("fields") or {}).get("issuelinks") or []
            wanted_type = link_type.strip().lower() if link_type else None
            matches: list[dict[str, Any]] = []
            for link in links:
                other = link.get("outwardIssue") or link.get("inwardIssue") or {}
                if other.get("key") != related_key:
                    continue
                type_name = (link.get("type") or {}).get("name")
                if wanted_type and str(type_name).strip().lower() != wanted_type:
                    continue
                matches.append(
                    {
                        "id": str(link.get("id")),
                        "type": type_name,
                        "direction": "outward" if link.get("outwardIssue") else "inward",
                    }
                )
            if not matches:
                suffix = f" of type '{link_type}'" if link_type else ""
                raise ValueError(
                    f"No link{suffix} between {issue_key} and {related_key}. "
                    "Check the pair with get_issue_for_review."
                )
            if wanted_type is None and len({m["type"] for m in matches}) > 1:
                return {
                    "status": "ambiguous_link_type",
                    "issue_key": issue_key,
                    "related_issue": related_key,
                    "candidates": sorted({str(m["type"]) for m in matches}),
                    "hint": (
                        "Several link types connect these issues. Pass link_type to choose which "
                        "to remove; do not guess."
                    ),
                    "url": _browse_url(profile, issue_key),
                }
            for match in matches:
                await adapter.delete_issue_link(match["id"])
            return {
                "status": "unlinked",
                "issue_key": issue_key,
                "related_issue": related_key,
                "removed": matches,
                "count": len(matches),
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def remove_from_sprint(issue_key_or_url: str, ctx: Context) -> dict[str, Any]:
    """Move an issue out of its sprint, back to the backlog (the undo of set_sprint).

    Uses the Agile backlog endpoint. Sub-tasks follow their parent's sprint, so target a
    standard issue — on a sub-task this has no independent effect.
    """
    del ctx
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            await adapter.move_to_backlog(issue_key)
            return {
                "issue_key": issue_key,
                "status": "removed_from_sprint",
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


@mcp.tool()
async def delete_worklog(
    issue_key_or_url: str,
    worklog_id: str,
    ctx: Context,
    adjust_estimate: str = "auto",
) -> dict[str, Any]:
    """Delete a worklog entry from an issue (the undo of log_work).

    `worklog_id` comes from list_worklogs. `adjust_estimate` is "auto" (give the deleted time
    back to the remaining estimate, default) or "leave" (keep it unchanged). Irreversible —
    re-add with log_work if needed.
    """
    del ctx
    if adjust_estimate not in _ADJUST_ESTIMATE_MODES:
        raise ValueError(
            f"adjust_estimate must be one of {_ADJUST_ESTIMATE_MODES}; got '{adjust_estimate}'."
        )
    worklog_id = str(worklog_id).strip()
    if not worklog_id:
        raise ValueError("delete_worklog needs a non-empty worklog_id; get it from list_worklogs.")
    try:
        profile, adapter, issue_key = await _resolve_issue_context(issue_key_or_url)
        try:
            await adapter.delete_worklog(issue_key, worklog_id, adjust_estimate=adjust_estimate)
            return {
                "issue_key": issue_key,
                "status": "worklog_deleted",
                "worklog_id": worklog_id,
                "url": _browse_url(profile, issue_key),
            }
        finally:
            await adapter.aclose()
    except (ConfigError, JiraApiError) as exc:
        raise _translate_error(exc) from exc


def main() -> None:
    mcp.run()
