from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from .config import JiraProfile


class JiraApiError(RuntimeError):
    pass


class JiraAdapter(Protocol):
    profile: JiraProfile

    async def aclose(self) -> None: ...

    async def get_issue(self, issue_key: str) -> dict[str, Any]: ...

    async def get_transitions(self, issue_key: str) -> dict[str, Any]: ...

    async def search_issues(
        self, jql: str, *, fields: Sequence[str], max_results: int = 50, start_at: int = 0
    ) -> dict[str, Any]: ...

    async def update_issue(
        self,
        issue_key: str,
        fields: dict[str, Any] | None = None,
        *,
        update: dict[str, Any] | None = None,
        notify_users: bool = True,
    ) -> None: ...

    async def add_comment(self, issue_key: str, body: str) -> dict[str, Any]: ...

    async def delete_comment(self, issue_key: str, comment_id: str) -> None: ...

    async def transition_issue(self, issue_key: str, transition_id: str) -> None: ...

    async def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]: ...

    def make_absolute_url(self, maybe_relative_url: str | None) -> str | None: ...

    def build_api_issue_url(self, issue_key: str) -> str: ...


class BaseJiraApiClient:
    api_version = "2"

    def __init__(self, profile: JiraProfile) -> None:
        self.profile = profile
        headers = {"Accept": "application/json"}
        if profile.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {profile.token}"
        else:
            if not profile.email:
                raise JiraApiError(
                    f"Jira profile '{profile.resolved_name}' uses basic auth but has no email configured."
                )
            credentials = base64.b64encode(f"{profile.email}:{profile.token}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"

        verify: bool | str = profile.verify_tls
        if profile.ca_bundle_path:
            verify = profile.ca_bundle_path

        self._client = httpx.AsyncClient(
            base_url=f"{profile.normalized_base_url}/rest/api/{self.api_version}",
            headers=headers,
            timeout=profile.timeout_seconds,
            verify=verify,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.request(method, path, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            if len(detail) > 300:
                detail = f"{detail[:300]}..."
            raise JiraApiError(
                f"Jira API request failed with {response.status_code}: {detail or exc!s}"
            ) from exc
        return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._send(method, path, **kwargs)
        return response.json()

    @staticmethod
    def _adf_doc(text: str) -> dict[str, Any]:
        return {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }

    def _comment_body(self, text: str) -> str | dict[str, Any]:
        # DC / API v2 accepts a plain string; Cloud / API v3 needs an ADF document.
        return self._adf_doc(text) if self.api_version == "3" else text

    def _prepare_fields(self, fields: dict[str, Any] | None) -> dict[str, Any] | None:
        # On Cloud / API v3 the rich-text `description` must be ADF, not a plain string.
        if not fields or self.api_version != "3":
            return fields
        if isinstance(fields.get("description"), str):
            return {**fields, "description": self._adf_doc(fields["description"])}
        return fields

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/issue/{issue_key}",
            params={
                "expand": "renderedFields,names",
                "fields": (
                    "*all,"
                    "comment,attachment,issuelinks,subtasks,summary,description,"
                    "issuetype,status,priority,assignee,reporter,labels,components,fixVersions"
                ),
            },
        )

    async def get_transitions(self, issue_key: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/issue/{issue_key}/transitions", params={"expand": "transitions.fields"}
        )

    async def search_issues(
        self, jql: str, *, fields: Sequence[str], max_results: int = 50, start_at: int = 0
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/search",
            params={
                "jql": jql,
                "fields": ",".join(fields),
                "maxResults": max_results,
                "startAt": start_at,
            },
        )

    async def update_issue(
        self,
        issue_key: str,
        fields: dict[str, Any] | None = None,
        *,
        update: dict[str, Any] | None = None,
        notify_users: bool = True,
    ) -> None:
        # PUT returns 204 No Content on success. `fields` sets values; `update` applies
        # add/remove verbs so multi-value fields are not clobbered.
        body: dict[str, Any] = {}
        prepared = self._prepare_fields(fields)
        if prepared:
            body["fields"] = prepared
        if update:
            body["update"] = update
        await self._send(
            "PUT",
            f"/issue/{issue_key}",
            params={"notifyUsers": "true" if notify_users else "false"},
            json=body,
        )

    async def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/issue/{issue_key}/comment", json={"body": self._comment_body(body)}
        )

    async def delete_comment(self, issue_key: str, comment_id: str) -> None:
        # DELETE returns 204 No Content on success.
        await self._send("DELETE", f"/issue/{issue_key}/comment/{comment_id}")

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        # POST returns 204 No Content on success. A comment is NOT attached here: many
        # transition screens have no comment field and would silently drop it — post the
        # comment separately via add_comment instead.
        await self._send(
            "POST",
            f"/issue/{issue_key}/transitions",
            json={"transition": {"id": str(transition_id)}},
        )

    async def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/issue", json={"fields": self._prepare_fields(fields)})

    def make_absolute_url(self, maybe_relative_url: str | None) -> str | None:
        if not maybe_relative_url:
            return None
        return urljoin(f"{self.profile.normalized_base_url}/", maybe_relative_url)

    def build_api_issue_url(self, issue_key: str) -> str:
        return f"{self.profile.normalized_base_url}/rest/api/{self.api_version}/issue/{issue_key}"


class JiraDataCenterAdapter(BaseJiraApiClient):
    api_version = "2"


class JiraCloudAdapter(BaseJiraApiClient):
    api_version = "3"


def build_jira_adapter(profile: JiraProfile) -> JiraAdapter:
    if profile.deployment == "dc":
        return JiraDataCenterAdapter(profile)
    if profile.deployment == "cloud":
        return JiraCloudAdapter(profile)
    raise JiraApiError(f"Unsupported Jira deployment type: {profile.deployment}")
