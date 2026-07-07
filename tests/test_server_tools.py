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
