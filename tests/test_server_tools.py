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

    async def update_issue(self, issue_key: str, fields: dict[str, Any], *, notify_users: bool = True) -> None:
        self.calls.append(("update", issue_key, fields, notify_users))

    async def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        self.calls.append(("comment", issue_key, body))
        return {"id": "555"}

    async def get_transitions(self, issue_key: str) -> dict[str, Any]:
        return {"transitions": [{"id": "31", "name": "Done"}]}

    async def transition_issue(self, issue_key: str, transition_id: str, *, comment: str | None = None) -> None:
        self.calls.append(("transition", issue_key, transition_id, comment))

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
    result = asyncio.run(_fn(server.update_issue)("BL-1", {"acceptance_criteria": "AC"}, None))
    assert fake.calls == [("update", "BL-1", {"customfield_5": "AC"}, True)]
    assert result["status"] == "updated"
    assert result["updated_fields"] == ["customfield_5"]
    assert result["url"].endswith("/browse/BL-1")


def test_add_comment_returns_comment_id(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.add_comment)("BL-1", "hi", None))
    assert fake.calls == [("comment", "BL-1", "hi")]
    assert result["comment_id"] == "555"


def test_transition_issue_resolves_name(fake: FakeAdapter) -> None:
    result = asyncio.run(_fn(server.transition_issue)("BL-1", "done", None, "note"))
    assert fake.calls == [("transition", "BL-1", "31", "note")]
    assert result["transition_id"] == "31"


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
    assert "project = MKT" in jql
