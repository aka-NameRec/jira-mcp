"""HTTP-shape tests for the Jira API adapter write methods, using httpx MockTransport.

No network access: a mock transport captures the outgoing request so we can assert the
exact method, path, query params and JSON body each write method produces.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from jira_mcp.config import JiraFieldMappings, JiraProfile
from jira_mcp.jira_api import JiraApiError, build_jira_adapter


async def _make_adapter(handler: Callable[[httpx.Request], httpx.Response], *, deployment: str = "dc"):
    profile = JiraProfile(
        base_url="https://jira.example.test",
        token="tok",
        deployment=deployment,
        auth_type="bearer",
        issue_key_prefixes=["BL"],
        field_mappings=JiraFieldMappings(acceptance_criteria="customfield_100"),
    )
    adapter = build_jira_adapter(profile)
    # Replace the real client with one backed by a mock transport, same base_url/version.
    await adapter.aclose()
    adapter._client = httpx.AsyncClient(
        base_url=f"{profile.normalized_base_url}/rest/api/{adapter.api_version}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {profile.token}"},
        transport=httpx.MockTransport(handler),
    )
    return adapter


def _capture(status: int, *, json_body: Any | None = None, text: str = "") -> tuple[dict[str, Any], Callable]:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content) if request.content else None
        if json_body is not None:
            return httpx.Response(status, json=json_body)
        return httpx.Response(status, text=text)

    return seen, handler


def test_update_issue_puts_fields_with_notify_param() -> None:
    seen, handler = _capture(204)

    async def scenario() -> Any:
        adapter = await _make_adapter(handler)
        try:
            return await adapter.update_issue("BL-1", {"summary": "New"}, notify_users=False)
        finally:
            await adapter.aclose()

    result = asyncio.run(scenario())
    assert seen["method"] == "PUT"
    assert seen["path"].endswith("/rest/api/2/issue/BL-1")
    assert seen["params"]["notifyUsers"] == "false"
    assert seen["body"] == {"fields": {"summary": "New"}}
    assert result is None  # 204 No Content, no body parsed


def test_update_issue_update_verb_only() -> None:
    seen, handler = _capture(204)

    async def scenario() -> None:
        adapter = await _make_adapter(handler)
        try:
            await adapter.update_issue("BL-1", None, update={"labels": [{"add": "x"}]})
        finally:
            await adapter.aclose()

    asyncio.run(scenario())
    assert seen["method"] == "PUT"
    assert seen["body"] == {"update": {"labels": [{"add": "x"}]}}


def test_update_issue_wraps_description_adf_on_cloud() -> None:
    seen, handler = _capture(204)

    async def scenario() -> None:
        adapter = await _make_adapter(handler, deployment="cloud")
        try:
            await adapter.update_issue("BL-1", {"description": "hi"})
        finally:
            await adapter.aclose()

    asyncio.run(scenario())
    body = seen["body"]["fields"]["description"]
    assert body["type"] == "doc"
    assert body["content"][0]["content"][0]["text"] == "hi"


def test_create_issue_wraps_description_adf_on_cloud() -> None:
    seen, handler = _capture(201, json_body={"key": "BL-2"})

    async def scenario() -> Any:
        adapter = await _make_adapter(handler, deployment="cloud")
        try:
            return await adapter.create_issue({"summary": "s", "description": "hi"})
        finally:
            await adapter.aclose()

    asyncio.run(scenario())
    assert seen["body"]["fields"]["description"]["type"] == "doc"


def test_add_comment_plain_body_for_dc() -> None:
    seen, handler = _capture(201, json_body={"id": "999"})

    async def scenario() -> Any:
        adapter = await _make_adapter(handler, deployment="dc")
        try:
            return await adapter.add_comment("BL-1", "hello")
        finally:
            await adapter.aclose()

    result = asyncio.run(scenario())
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/rest/api/2/issue/BL-1/comment")
    assert seen["body"] == {"body": "hello"}  # DC / API v2 uses a plain string
    assert result == {"id": "999"}


def test_add_comment_adf_body_for_cloud() -> None:
    seen, handler = _capture(201, json_body={"id": "1"})

    async def scenario() -> Any:
        adapter = await _make_adapter(handler, deployment="cloud")
        try:
            return await adapter.add_comment("BL-1", "hi")
        finally:
            await adapter.aclose()

    asyncio.run(scenario())
    assert seen["path"].endswith("/rest/api/3/issue/BL-1/comment")
    body = seen["body"]["body"]
    assert body["type"] == "doc" and body["version"] == 1
    assert body["content"][0]["content"][0]["text"] == "hi"


def test_delete_comment_issues_delete() -> None:
    seen, handler = _capture(204)

    async def scenario() -> None:
        adapter = await _make_adapter(handler)
        try:
            await adapter.delete_comment("BL-1", "555")
        finally:
            await adapter.aclose()

    asyncio.run(scenario())
    assert seen["method"] == "DELETE"
    assert seen["path"].endswith("/rest/api/2/issue/BL-1/comment/555")


def test_transition_issue_posts_transition_only() -> None:
    seen, handler = _capture(204)

    async def scenario() -> None:
        adapter = await _make_adapter(handler)
        try:
            await adapter.transition_issue("BL-1", "5")
        finally:
            await adapter.aclose()

    asyncio.run(scenario())
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/rest/api/2/issue/BL-1/transitions")
    # No comment embedded in the transition payload (posted separately by the tool).
    assert seen["body"] == {"transition": {"id": "5"}}


def test_get_transitions_reads_with_expand() -> None:
    seen, handler = _capture(200, json_body={"transitions": [{"id": "1", "name": "Start"}]})

    async def scenario() -> Any:
        adapter = await _make_adapter(handler)
        try:
            return await adapter.get_transitions("BL-1")
        finally:
            await adapter.aclose()

    result = asyncio.run(scenario())
    assert seen["method"] == "GET"
    assert seen["path"].endswith("/rest/api/2/issue/BL-1/transitions")
    assert seen["params"]["expand"] == "transitions.fields"
    assert result["transitions"][0]["name"] == "Start"


def test_create_issue_posts_fields() -> None:
    seen, handler = _capture(201, json_body={"id": "10", "key": "BL-2"})

    async def scenario() -> Any:
        adapter = await _make_adapter(handler)
        try:
            return await adapter.create_issue({"project": {"key": "BL"}, "summary": "S"})
        finally:
            await adapter.aclose()

    result = asyncio.run(scenario())
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/rest/api/2/issue")
    assert seen["body"] == {"fields": {"project": {"key": "BL"}, "summary": "S"}}
    assert result["key"] == "BL-2"


def test_search_issues_reads_with_params() -> None:
    seen, handler = _capture(200, json_body={"issues": [], "total": 0})

    async def scenario() -> Any:
        adapter = await _make_adapter(handler)
        try:
            return await adapter.search_issues(
                "assignee = currentUser()", fields=["summary", "status"], max_results=25
            )
        finally:
            await adapter.aclose()

    result = asyncio.run(scenario())
    assert seen["method"] == "GET"
    assert seen["path"].endswith("/rest/api/2/search")
    assert seen["params"]["jql"] == "assignee = currentUser()"
    assert seen["params"]["fields"] == "summary,status"
    assert seen["params"]["maxResults"] == "25"
    assert result["total"] == 0


def test_get_myself_reads() -> None:
    seen, handler = _capture(200, json_body={"name": "u", "displayName": "U"})

    async def scenario() -> Any:
        adapter = await _make_adapter(handler)
        try:
            return await adapter.get_myself()
        finally:
            await adapter.aclose()

    result = asyncio.run(scenario())
    assert seen["method"] == "GET"
    assert seen["path"].endswith("/rest/api/2/myself")
    assert result["name"] == "u"


def test_get_create_meta_reads_with_params() -> None:
    seen, handler = _capture(200, json_body={"projects": []})

    async def scenario() -> None:
        adapter = await _make_adapter(handler)
        try:
            await adapter.get_create_meta("BL", expand="projects.issuetypes")
        finally:
            await adapter.aclose()

    asyncio.run(scenario())
    assert seen["path"].endswith("/rest/api/2/issue/createmeta")
    assert seen["params"]["projectKeys"] == "BL"
    assert seen["params"]["expand"] == "projects.issuetypes"


def test_non_2xx_raises_jira_api_error() -> None:
    _, handler = _capture(400, text="Field 'summary' is required")

    async def scenario() -> None:
        adapter = await _make_adapter(handler)
        try:
            await adapter.update_issue("BL-1", {"summary": ""})
        finally:
            await adapter.aclose()

    with pytest.raises(JiraApiError) as exc:
        asyncio.run(scenario())
    assert "400" in str(exc.value)
