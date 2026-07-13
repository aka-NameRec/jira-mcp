"""Orchestration tests for the write tools: profile resolution + adapter wiring + result shape.

A fake adapter records calls, so these tests assert the tool layer translates field aliases,
resolves transitions by name, and shapes results, without any real HTTP.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import jira_mcp.server as server
from jira_mcp.config import JiraFieldMappings, JiraProfile


class FakeAdapter:
    def __init__(self, profile: JiraProfile) -> None:
        self.profile = profile
        self.calls: list[tuple[Any, ...]] = []

    async def aclose(self) -> None:
        return None

    async def update_issue(
        self,
        issue_key: str,
        fields: dict[str, Any] | None = None,
        *,
        update: dict[str, Any] | None = None,
        notify_users: bool = True,
    ) -> None:
        self.calls.append(("update", issue_key, fields, update, notify_users))

    async def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        self.calls.append(("comment", issue_key, body))
        return {"id": "555"}

    async def delete_comment(self, issue_key: str, comment_id: str) -> None:
        self.calls.append(("delete_comment", issue_key, comment_id))

    async def get_transitions(self, issue_key: str) -> dict[str, Any]:
        return {"transitions": [{"id": "31", "name": "Done"}]}

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        self.calls.append(("transition", issue_key, transition_id))

    async def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", fields))
        return {"id": "9", "key": "BL-9"}

    async def search_issues(self, jql: str, *, fields, max_results: int = 50, start_at: int = 0) -> dict[str, Any]:
        self.calls.append(("search", jql, max_results))
        return {
            "issues": [
                {
                    "key": "BL-1",
                    "fields": {
                        "summary": "S",
                        "status": {"name": "В работе"},
                        "issuetype": {"name": "Dev SubTask"},
                        "priority": {"name": "Low"},
                        "project": {"key": "BL"},
                        "updated": "2026-07-08T00:00:00.000+0000",
                    },
                }
            ]
        }

    async def get_myself(self) -> dict[str, Any]:
        return {
            "name": "asataev@devcats.kg",
            "displayName": "Adilet Sataev",
            "emailAddress": "asataev@devcats.kg",
        }

    async def add_worklog(
        self,
        issue_key: str,
        *,
        time_spent: str,
        comment: str | None = None,
        started: str | None = None,
        adjust_estimate: str = "auto",
    ) -> dict[str, Any]:
        self.calls.append(("worklog", issue_key, time_spent, comment, started, adjust_estimate))
        return {
            "id": "700",
            "timeSpent": time_spent,
            "timeSpentSeconds": 10800,
            "started": started or "2026-07-08T10:00:00.000+0600",
        }

    async def get_worklogs(self, issue_key: str) -> dict[str, Any]:
        return {
            "total": 1,
            "worklogs": [
                {
                    "id": "700",
                    "author": {"displayName": "Adilet Sataev"},
                    "timeSpent": "3h",
                    "timeSpentSeconds": 10800,
                    "started": "2026-07-08T10:00:00.000+0600",
                    "comment": "did work",
                }
            ],
        }

    async def search_users(self, query: str, *, max_results: int = 20) -> list[dict[str, Any]]:
        self.calls.append(("search_users", query, max_results))
        return [
            {
                "name": "aomurkulov@devcats.kg",
                "displayName": "Aidin Omurkulov",
                "emailAddress": "aomurkulov@devcats.kg",
                "active": True,
            }
        ]

    async def assign_issue(self, issue_key: str, assignee: str | None) -> None:
        self.calls.append(("assign", issue_key, assignee))

    async def get_link_types(self) -> dict[str, Any]:
        return {
            "issueLinkTypes": [
                {"id": "10000", "name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
                {"id": "10001", "name": "Relates", "outward": "relates to", "inward": "relates to"},
            ]
        }

    async def create_issue_link(
        self,
        link_type: str,
        *,
        inward_issue: str,
        outward_issue: str,
        comment: str | None = None,
    ) -> None:
        self.calls.append(("link", link_type, outward_issue, inward_issue, comment))

    async def get_priorities(self) -> list[dict[str, Any]]:
        return [{"id": "2", "name": "High"}, {"id": "1", "name": "Highest"}]

    async def get_boards(self, project_key: str, *, max_results: int = 50) -> dict[str, Any]:
        return {
            "total": 2,
            "isLast": True,
            "values": [
                {"id": 289, "name": "BL board", "type": "kanban"},
                {"id": 414, "name": "BL Sprints", "type": "scrum"},
            ],
        }

    async def get_board_sprints(
        self, board_id: int | str, *, state: str | None = None, max_results: int = 50
    ) -> dict[str, Any]:
        self.calls.append(("board_sprints", board_id, state))
        return {
            "isLast": True,
            "values": [
                {
                    "id": 77,
                    "name": "Sprint 7",
                    "state": "active",
                    "startDate": "2026-07-01T00:00:00.000Z",
                    "endDate": "2026-07-14T00:00:00.000Z",
                }
            ],
        }

    async def add_issue_to_sprint(self, sprint_id: int | str, issue_key: str) -> None:
        self.calls.append(("set_sprint", sprint_id, issue_key))

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        # BL-2 has two links (Blocks + Duplicate) to exercise the ambiguous path; BL-3 has one.
        return {
            "key": issue_key,
            "fields": {
                "issuelinks": [
                    {
                        "id": "1001",
                        "type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
                        "outwardIssue": {"key": "BL-2"},
                    },
                    {
                        "id": "1002",
                        "type": {"name": "Relates", "outward": "relates to", "inward": "relates to"},
                        "inwardIssue": {"key": "BL-3"},
                    },
                    {
                        "id": "1003",
                        "type": {"name": "Duplicate", "outward": "duplicates", "inward": "is duplicated by"},
                        "outwardIssue": {"key": "BL-2"},
                    },
                ]
            },
        }

    async def delete_issue_link(self, link_id: str) -> None:
        self.calls.append(("delete_link", link_id))

    async def move_to_backlog(self, issue_key: str) -> None:
        self.calls.append(("backlog", issue_key))

    async def delete_worklog(
        self, issue_key: str, worklog_id: str, *, adjust_estimate: str = "auto"
    ) -> None:
        self.calls.append(("delete_worklog", issue_key, worklog_id, adjust_estimate))

    async def get_create_meta(self, project_key: str, *, expand: str | None = None) -> dict[str, Any]:
        return {
            "projects": [
                {
                    "key": project_key,
                    "issuetypes": [
                        {
                            "id": "10",
                            "name": "Dev SubTask",
                            "subtask": True,
                            "fields": {
                                "summary": {"name": "Summary", "required": True},
                                "parent": {"name": "Parent", "required": True},
                                "labels": {"name": "Labels", "required": False},
                            },
                        },
                        {
                            "id": "11",
                            "name": "Dev Task",
                            "subtask": False,
                            "fields": {"summary": {"name": "Summary", "required": True}},
                        },
                    ],
                }
            ]
        }


def _fn(tool: Any):
    """Return the underlying coroutine function whether mcp.tool() returns it raw or wrapped."""
    return getattr(tool, "fn", tool)


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    profile = JiraProfile(
        base_url="https://jira.example.test",
        token="tok",
        issue_key_prefixes=["BL"],
        field_mappings=JiraFieldMappings(acceptance_criteria="customfield_5"),
    )
    adapter = FakeAdapter(profile)
    monkeypatch.setattr(server, "resolve_profile_for_issue_key", lambda key: profile)
    monkeypatch.setattr(server, "resolve_profile_for_url", lambda url: profile)
    monkeypatch.setattr(server, "build_jira_adapter", lambda prof: adapter)
    monkeypatch.setattr(server, "load_jira_profiles", lambda: [profile])
    return adapter


def test_update_issue_translates_alias(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.update_issue)("BL-1", None, {"acceptance_criteria": "AC"}))
    assert fake.calls == [("update", "BL-1", {"customfield_5": "AC"}, None, True)]
    assert result["status"] == "updated"
    assert result["updated_fields"] == ["customfield_5"]
    assert result["url"].endswith("/browse/BL-1")


def test_update_issue_add_remove_uses_update_verb(fake: FakeAdapter) -> None:
    result = asyncio.run(
        _fn(server.update_issue)("BL-1", None, None, {"labels": ["china"]}, {"labels": ["old"]})
    )
    assert fake.calls == [
        ("update", "BL-1", None, {"labels": [{"add": "china"}, {"remove": "old"}]}, True)
    ]
    assert result["updated_fields"] == ["labels"]


def test_update_issue_requires_something_to_change(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="at least one of"):
        asyncio.run(_fn(server.update_issue)("BL-1", None))
    assert fake.calls == []


def test_update_issue_add_scalar_is_normalized(fake: FakeAdapter) -> None:
    # A scalar (not a list) must not be iterated char-by-char.
    asyncio.run(_fn(server.update_issue)("BL-1", None, None, {"labels": "china"}))
    assert fake.calls == [("update", "BL-1", None, {"labels": [{"add": "china"}]}, True)]


def test_add_comment_returns_comment_id(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.add_comment)("BL-1", "hi", None))
    assert fake.calls == [("comment", "BL-1", "hi")]
    assert result["comment_id"] == "555"


def test_delete_comment_tool(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.delete_comment)("BL-1", "555", None))
    assert fake.calls == [("delete_comment", "BL-1", "555")]
    assert result["status"] == "comment_deleted"
    assert result["comment_id"] == "555"


def test_transition_issue_resolves_name_and_comments_separately(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.transition_issue)("BL-1", "done", None, "note"))
    # Transition first, then a separate add_comment (not embedded in the transition payload).
    assert fake.calls == [("transition", "BL-1", "31"), ("comment", "BL-1", "note")]
    assert result["transition_id"] == "31"
    assert result["commented"] is True


def test_transition_issue_without_comment_skips_add_comment(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.transition_issue)("BL-1", "31", None))
    assert fake.calls == [("transition", "BL-1", "31")]
    assert result["commented"] is False


def test_list_transitions_shapes_result(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.list_transitions)("BL-1", None))
    assert result["issue_key"] == "BL-1"
    assert result["transitions"] == [{"id": "31", "name": "Done", "to_status": None}]
    assert result["url"].endswith("/browse/BL-1")


def test_create_issue_builds_payload_with_alias(fake: FakeAdapter) -> None:
    result = asyncio.run(
        _fn(server.create_issue)("bl", "Task", "Summary", None, "desc", {"acceptance_criteria": "AC"})
    )
    assert fake.calls[0][0] == "create"
    payload = fake.calls[0][1]
    assert payload["project"] == {"key": "BL"}
    assert payload["issuetype"] == {"name": "Task"}
    assert payload["summary"] == "Summary"
    assert payload["description"] == "desc"
    assert payload["customfield_5"] == "AC"
    assert result["issue_key"] == "BL-9"


def test_my_issues_default_jql_and_shape(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.my_issues)(None))
    jql = fake.calls[0][1]
    assert fake.calls[0][0] == "search"
    assert "assignee = currentUser()" in jql
    assert "statusCategory != Done" in jql  # only_open defaults to True
    assert result["count"] == 1
    issue = result["issues"][0]
    assert issue["key"] == "BL-1"
    assert issue["project"] == "BL"
    assert issue["status"] == "В работе"
    assert issue["url"].endswith("/browse/BL-1")


def test_my_issues_all_and_project_filter(fake: FakeAdapter) -> None:
    asyncio.run(_fn(server.my_issues)(None, only_open=False, project="mkt"))
    jql = fake.calls[0][1]
    assert "statusCategory != Done" not in jql  # only_open=False drops the filter
    assert 'project = "MKT"' in jql


def test_my_issues_sorts_merge_across_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    # A genuine multi-profile merge is re-sorted by `updated` desc so the union is ordered,
    # not just grouped by profile.
    p_a = JiraProfile(base_url="https://a.test", token="t", issue_key_prefixes=["A"])
    p_b = JiraProfile(base_url="https://b.test", token="t", issue_key_prefixes=["B"])

    def make(profile: JiraProfile, key: str, updated: str):
        class Ok:
            def __init__(self, prof: JiraProfile) -> None:
                self.profile = prof

            async def aclose(self) -> None:
                return None

            async def search_issues(self, jql, *, fields, max_results=50, start_at=0):
                return {"issues": [{"key": key, "fields": {"project": {"key": key[0]}, "updated": updated}}]}

        return Ok(profile)

    adapters = {
        p_a.normalized_base_url: make(p_a, "A-1", "2026-01-01"),
        p_b.normalized_base_url: make(p_b, "B-1", "2026-03-01"),
    }
    monkeypatch.setattr(server, "load_jira_profiles", lambda: [p_a, p_b])
    monkeypatch.setattr(server, "build_jira_adapter", lambda prof: adapters[prof.normalized_base_url])

    result = asyncio.run(_fn(server.my_issues)(None))
    assert [i["key"] for i in result["issues"]] == ["B-1", "A-1"]  # newer first across profiles
    assert result["errors"] == []


def test_my_issues_isolates_profile_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from jira_mcp.jira_api import JiraApiError

    p_ok = JiraProfile(base_url="https://a.test", token="t", issue_key_prefixes=["A"])
    p_bad = JiraProfile(base_url="https://b.test", token="t", issue_key_prefixes=["B"])

    class Ok:
        def __init__(self, profile: JiraProfile) -> None:
            self.profile = profile

        async def aclose(self) -> None:
            return None

        async def search_issues(self, jql, *, fields, max_results=50, start_at=0):
            return {"issues": [{"key": "A-1", "fields": {"project": {"key": "A"}, "updated": "2026-01-01"}}]}

    class Bad:
        def __init__(self, profile: JiraProfile) -> None:
            self.profile = profile

        async def aclose(self) -> None:
            return None

        async def search_issues(self, *a, **k):
            raise JiraApiError("boom")

    adapters = {p_ok.normalized_base_url: Ok(p_ok), p_bad.normalized_base_url: Bad(p_bad)}
    monkeypatch.setattr(server, "load_jira_profiles", lambda: [p_ok, p_bad])
    monkeypatch.setattr(server, "build_jira_adapter", lambda prof: adapters[prof.normalized_base_url])

    result = asyncio.run(_fn(server.my_issues)(None))
    assert [i["key"] for i in result["issues"]] == ["A-1"]  # good profile still returned
    assert len(result["errors"]) == 1  # bad profile isolated, not fatal
    assert result["errors"][0]["profile"] == p_bad.resolved_name


def test_whoami_lists_current_user(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.whoami)(None))
    assert result["users"][0]["name"] == "asataev@devcats.kg"
    assert result["users"][0]["display_name"] == "Adilet Sataev"
    assert result["errors"] == []


def test_list_issue_types(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.list_issue_types)("BL", None))
    by_name = {t["name"]: t for t in result["issue_types"]}
    assert "Dev SubTask" in by_name and "Dev Task" in by_name
    assert by_name["Dev SubTask"]["subtask"] is True
    assert by_name["Dev Task"]["subtask"] is False


def test_get_create_metadata_required_fields(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.get_create_metadata)("BL", "Dev SubTask", None))
    assert {f["id"] for f in result["required_fields"]} == {"summary", "parent"}


def test_get_create_metadata_unknown_type_raises(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(_fn(server.get_create_metadata)("BL", "Nope", None))


def test_get_create_metadata_resolves_by_id(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.get_create_metadata)("BL", "10", None))  # Dev SubTask id
    assert {f["id"] for f in result["required_fields"]} == {"summary", "parent"}


def test_transition_issue_comment_failure_is_reported(
    fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jira_mcp.jira_api import JiraApiError

    async def boom(issue_key: str, body: str) -> dict[str, Any]:
        raise JiraApiError("transition screen has no comment field")

    monkeypatch.setattr(fake, "add_comment", boom)
    result = asyncio.run(_fn(server.transition_issue)("BL-1", "done", None, "note"))
    # Transition already landed; comment failure is reported, not raised.
    assert result["status"] == "transitioned"
    assert result["commented"] is False
    assert "comment_error" in result
    assert ("transition", "BL-1", "31") in fake.calls


def test_create_issue_missing_key_url_none(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_key(fields: dict[str, Any]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(fake, "create_issue", no_key)
    result = asyncio.run(_fn(server.create_issue)("BL", "Dev Task", "S", None))
    assert result["issue_key"] is None
    assert result["url"] is None


def test_my_issues_reports_truncation(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def big(jql: str, *, fields, max_results: int = 50, start_at: int = 0) -> dict[str, Any]:
        return {"issues": [{"key": "BL-1", "fields": {"updated": "2026-01-01"}}], "total": 99}

    monkeypatch.setattr(fake, "search_issues", big)
    result = asyncio.run(_fn(server.my_issues)(None))
    assert result["truncated"] is True


def test_whoami_isolates_profile_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from jira_mcp.jira_api import JiraApiError

    p_ok = JiraProfile(base_url="https://a.test", token="t", issue_key_prefixes=["A"])
    p_bad = JiraProfile(base_url="https://b.test", token="t", issue_key_prefixes=["B"])

    class Ok:
        def __init__(self, profile: JiraProfile) -> None:
            self.profile = profile

        async def aclose(self) -> None:
            return None

        async def get_myself(self) -> dict[str, Any]:
            return {"name": "me@x", "displayName": "Me"}

    class Bad:
        def __init__(self, profile: JiraProfile) -> None:
            self.profile = profile

        async def aclose(self) -> None:
            return None

        async def get_myself(self) -> dict[str, Any]:
            raise JiraApiError("401 Unauthorized")

    adapters = {p_ok.normalized_base_url: Ok(p_ok), p_bad.normalized_base_url: Bad(p_bad)}
    monkeypatch.setattr(server, "load_jira_profiles", lambda: [p_ok, p_bad])
    monkeypatch.setattr(server, "build_jira_adapter", lambda prof: adapters[prof.normalized_base_url])

    result = asyncio.run(_fn(server.whoami)(None))
    assert [u["name"] for u in result["users"]] == ["me@x"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["profile"] == p_bad.resolved_name


def test_log_work_shapes_result(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.log_work)("BL-1", "3h", None, "did work"))
    assert fake.calls == [("worklog", "BL-1", "3h", "did work", None, "auto")]
    assert result["status"] == "worklog_added"
    assert result["worklog_id"] == "700"
    assert result["time_spent"] == "3h"
    assert result["time_spent_seconds"] == 10800
    assert result["url"].endswith("/browse/BL-1")


def test_log_work_passes_leave_adjust_estimate(fake: FakeAdapter) -> None:
    asyncio.run(_fn(server.log_work)("BL-1", "1h", None, adjust_estimate="leave"))
    assert fake.calls[0] == ("worklog", "BL-1", "1h", None, None, "leave")


def test_log_work_rejects_bad_adjust_estimate(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="adjust_estimate must be one of"):
        asyncio.run(_fn(server.log_work)("BL-1", "1h", None, adjust_estimate="manual"))
    assert fake.calls == []


def test_log_work_rejects_empty_time(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="non-empty time_spent"):
        asyncio.run(_fn(server.log_work)("BL-1", "  ", None))
    assert fake.calls == []


def test_list_worklogs_shapes(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.list_worklogs)("BL-1", None))
    assert result["count"] == 1
    assert result["total_time_spent_seconds"] == 10800
    entry = result["worklogs"][0]
    assert entry["author"] == "Adilet Sataev"
    assert entry["time_spent"] == "3h"


def test_search_users_across_profiles(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.search_users)("omurkulov", None))
    assert result["query"] == "omurkulov"
    assert result["count"] == 1
    user = result["users"][0]
    assert user["assignee"] == "aomurkulov@devcats.kg"  # `name` on DC -> what assign_issue takes
    assert user["display_name"] == "Aidin Omurkulov"
    assert user["email"] == "aomurkulov@devcats.kg"


def test_search_users_rejects_empty(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="non-empty query"):
        asyncio.run(_fn(server.search_users)("  ", None))


def test_assign_issue_tool(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.assign_issue)("BL-1", "aomurkulov@devcats.kg", None))
    assert fake.calls == [("assign", "BL-1", "aomurkulov@devcats.kg")]
    assert result["status"] == "assigned"
    assert result["assignee"] == "aomurkulov@devcats.kg"


def test_assign_issue_rejects_empty(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="non-empty assignee"):
        asyncio.run(_fn(server.assign_issue)("BL-1", "", None))
    assert fake.calls == []


def test_unassign_issue_tool(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.unassign_issue)("BL-1", None))
    assert fake.calls == [("assign", "BL-1", None)]  # None clears the assignee
    assert result["status"] == "unassigned"


def test_find_issues_by_assignee_single_match(fake: FakeAdapter) -> None:
    result = asyncio.run(
        _fn(server.find_issues)(None, assignee="Aidin Omurkulov", status_category="in progress")
    )
    assert result["status"] == "ok"
    assert result["matched_user"]["assignee"] == "aomurkulov@devcats.kg"
    # JQL resolved to the username and mapped the friendly category.
    assert 'assignee = "aomurkulov@devcats.kg"' in result["jql"]
    assert 'statusCategory = "In Progress"' in result["jql"]
    assert result["issues"][0]["key"] == "BL-1"


def test_find_issues_assignee_not_found(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(query: str, *, max_results: int = 20) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(fake, "search_users", none)
    result = asyncio.run(_fn(server.find_issues)(None, assignee="Nobody"))
    assert result["status"] == "assignee_not_found"
    assert result["issues"] == []
    assert result["candidates"] == []
    assert "hint" in result


def test_find_issues_assignee_ambiguous(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def many(query: str, *, max_results: int = 20) -> list[dict[str, Any]]:
        return [
            {"name": "aomurkulov@devcats.kg", "displayName": "Aidin Omurkulov"},
            {"name": "momurkulova@obank.kg", "displayName": "Malika Omurkulova"},
        ]

    monkeypatch.setattr(fake, "search_users", many)
    result = asyncio.run(_fn(server.find_issues)(None, assignee="Omurkulov"))
    assert result["status"] == "assignee_ambiguous"
    assert len(result["candidates"]) == 2
    assert result["issues"] == []
    assert "hint" in result


def test_find_issues_broad_without_assignee(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.find_issues)(None, project="bl", status_category="done"))
    assert result["status"] == "ok"
    assert 'project = "BL"' in result["jql"]
    assert 'statusCategory = "Done"' in result["jql"]
    assert result["issues"][0]["key"] == "BL-1"


def test_list_link_types_shapes(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.list_link_types)(None))
    by_name = {t["name"]: t for t in result["link_types"]}
    assert by_name["Blocks"]["outward"] == "blocks"
    assert by_name["Blocks"]["inward"] == "is blocked by"
    assert result["count"] == 2


def test_link_issues_by_name(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.link_issues)("BL-1", "BL-2", "Blocks", None))
    assert ("link", "Blocks", "BL-1", "BL-2", None) in fake.calls
    assert result["status"] == "linked"
    assert result["outward_issue"] == "BL-1"
    assert result["inward_issue"] == "BL-2"
    assert result["relationship"] == "BL-1 blocks BL-2"


def test_link_issues_inward_phrase_swaps(fake: FakeAdapter) -> None:
    # "BL-1 is blocked by BL-2" == "BL-2 blocks BL-1": the issues swap for the API call.
    result = asyncio.run(_fn(server.link_issues)("BL-1", "BL-2", "is blocked by", None))
    assert ("link", "Blocks", "BL-2", "BL-1", None) in fake.calls
    assert result["outward_issue"] == "BL-2"
    assert result["inward_issue"] == "BL-1"
    assert result["relationship"] == "BL-2 blocks BL-1"


def test_link_issues_unknown_type_raises(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="No link type matching"):
        asyncio.run(_fn(server.link_issues)("BL-1", "BL-2", "Nope", None))


def test_link_issues_cross_instance_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    p_a = JiraProfile(base_url="https://a.test", token="t", issue_key_prefixes=["A"])
    p_b = JiraProfile(base_url="https://b.test", token="t", issue_key_prefixes=["B"])
    profiles = {"A": p_a, "B": p_b}
    monkeypatch.setattr(
        server, "resolve_profile_for_issue_key", lambda key: profiles[key.split("-")[0]]
    )
    with pytest.raises(ValueError, match="same Jira instance"):
        asyncio.run(_fn(server.link_issues)("A-1", "B-2", "Blocks", None))


def test_list_priorities_shapes(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.list_priorities)(None))
    names = [p["name"] for p in result["priorities"]]
    assert "High" in names and "Highest" in names
    assert result["count"] == 2


def test_find_sprints_filters_scrum_and_shapes(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.find_sprints)("BL", None))
    assert result["status"] == "ok"
    assert result["count"] == 1
    sprint = result["sprints"][0]
    assert sprint["id"] == 77
    assert sprint["board_name"] == "BL Sprints"
    # Only the scrum board (414) was queried for sprints; the kanban board (289) was skipped.
    board_calls = [c for c in fake.calls if c[0] == "board_sprints"]
    assert board_calls == [("board_sprints", 414, "active,future")]


def test_find_sprints_state_all_expands(fake: FakeAdapter) -> None:
    asyncio.run(_fn(server.find_sprints)("BL", None, state="all"))
    board_calls = [c for c in fake.calls if c[0] == "board_sprints"]
    assert board_calls[0][2] == "active,future,closed"


def test_find_sprints_rejects_bad_state(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="state must be"):
        asyncio.run(_fn(server.find_sprints)("BL", None, state="soon"))


def test_find_sprints_no_scrum_board(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def only_kanban(project_key: str, *, max_results: int = 50) -> dict[str, Any]:
        return {"values": [{"id": 1, "name": "K", "type": "kanban"}]}

    monkeypatch.setattr(fake, "get_boards", only_kanban)
    result = asyncio.run(_fn(server.find_sprints)("BL", None))
    assert result["status"] == "no_board"
    assert result["sprints"] == []
    assert "hint" in result


def test_find_sprints_no_sprints(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(board_id: Any, *, state: str | None = None, max_results: int = 50) -> dict[str, Any]:
        return {"isLast": True, "values": []}

    monkeypatch.setattr(fake, "get_board_sprints", empty)
    result = asyncio.run(_fn(server.find_sprints)("BL", None))
    assert result["status"] == "no_sprints"
    assert result["sprints"] == []


def test_find_sprints_reports_boards_truncation(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    async def paged(project_key: str, *, max_results: int = 50) -> dict[str, Any]:
        return {"isLast": False, "values": [{"id": 414, "name": "BL Sprints", "type": "scrum"}]}

    monkeypatch.setattr(fake, "get_boards", paged)
    result = asyncio.run(_fn(server.find_sprints)("BL", None))
    assert result["status"] == "ok"
    assert result["boards_truncated"] is True


def test_find_sprints_no_board_flags_truncation(fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    # A scrum board could sit beyond the fetched page, so "no_board" must not read as certain.
    async def paged_kanban(project_key: str, *, max_results: int = 50) -> dict[str, Any]:
        return {"isLast": False, "values": [{"id": 1, "name": "K", "type": "kanban"}]}

    monkeypatch.setattr(fake, "get_boards", paged_kanban)
    result = asyncio.run(_fn(server.find_sprints)("BL", None))
    assert result["status"] == "no_board"
    assert result["boards_truncated"] is True


def test_set_sprint_tool(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.set_sprint)("BL-1", 77, None))
    assert ("set_sprint", "77", "BL-1") in fake.calls  # numeric id stringified
    assert result["status"] == "sprint_set"
    assert result["sprint_id"] == "77"
    assert result["url"].endswith("/browse/BL-1")


def test_set_sprint_rejects_non_numeric(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="numeric sprint id"):
        asyncio.run(_fn(server.set_sprint)("BL-1", "Sprint 7", None))
    assert fake.calls == []


def test_unlink_issues_single_match(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.unlink_issues)("BL-1", "BL-3", None))
    assert ("delete_link", "1002") in fake.calls  # the one Relates link to BL-3
    assert result["status"] == "unlinked"
    assert result["count"] == 1
    assert result["removed"][0]["direction"] == "inward"


def test_unlink_issues_ambiguous_type_asks(fake: FakeAdapter) -> None:
    # BL-2 is connected by both Blocks and Duplicate -> refuse and list, don't guess.
    result = asyncio.run(_fn(server.unlink_issues)("BL-1", "BL-2", None))
    assert result["status"] == "ambiguous_link_type"
    assert set(result["candidates"]) == {"Blocks", "Duplicate"}
    assert not any(c[0] == "delete_link" for c in fake.calls)  # nothing deleted


def test_unlink_issues_type_disambiguates(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.unlink_issues)("BL-1", "BL-2", None, "Blocks"))
    assert [c for c in fake.calls if c[0] == "delete_link"] == [("delete_link", "1001")]
    assert result["count"] == 1


def test_unlink_issues_no_link_raises(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="No link"):
        asyncio.run(_fn(server.unlink_issues)("BL-1", "BL-999", None))


def test_remove_from_sprint_tool(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.remove_from_sprint)("BL-1", None))
    assert ("backlog", "BL-1") in fake.calls
    assert result["status"] == "removed_from_sprint"
    assert result["url"].endswith("/browse/BL-1")


def test_delete_worklog_tool(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.delete_worklog)("BL-1", "700", None))
    assert ("delete_worklog", "BL-1", "700", "auto") in fake.calls
    assert result["status"] == "worklog_deleted"
    assert result["worklog_id"] == "700"


def test_delete_worklog_leave_mode(fake: FakeAdapter) -> None:
    asyncio.run(_fn(server.delete_worklog)("BL-1", "700", None, adjust_estimate="leave"))
    assert fake.calls[-1] == ("delete_worklog", "BL-1", "700", "leave")


def test_delete_worklog_rejects_bad_adjust(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="adjust_estimate must be one of"):
        asyncio.run(_fn(server.delete_worklog)("BL-1", "700", None, adjust_estimate="manual"))
    assert fake.calls == []


def test_delete_worklog_rejects_empty_id(fake: FakeAdapter) -> None:
    with pytest.raises(ValueError, match="non-empty worklog_id"):
        asyncio.run(_fn(server.delete_worklog)("BL-1", "  ", None))
    assert fake.calls == []


def test_find_issues_status_category_russian_alias(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.find_issues)(None, status_category="в процессе"))
    assert 'statusCategory = "In Progress"' in result["jql"]


def test_find_issues_order_by_whitelist_falls_back(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.find_issues)(None, order_by="; DROP TABLE"))
    assert result["jql"].endswith("ORDER BY updated DESC")  # unknown field -> safe default


def test_find_issues_broad_preserves_jql_order_single_profile(
    fake: FakeAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def two(jql: str, *, fields, max_results: int = 50, start_at: int = 0) -> dict[str, Any]:
        # Returned in the server-side JQL order: older first, newer second.
        return {
            "issues": [
                {"key": "BL-9", "fields": {"project": {"key": "BL"}, "updated": "2026-01-01"}},
                {"key": "BL-1", "fields": {"project": {"key": "BL"}, "updated": "2026-09-01"}},
            ]
        }

    monkeypatch.setattr(fake, "search_issues", two)
    result = asyncio.run(_fn(server.find_issues)(None, order_by="priority ASC"))
    # A single profile keeps the JQL order; it must NOT be re-sorted by `updated` desc.
    assert [i["key"] for i in result["issues"]] == ["BL-9", "BL-1"]
    assert result["jql"].endswith("ORDER BY priority ASC")


def test_find_issues_single_match_reports_resolve_errors(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.find_issues)(None, assignee="Aidin Omurkulov"))
    assert result["status"] == "ok"
    assert "errors" in result  # success branch must not swallow per-profile resolve errors
