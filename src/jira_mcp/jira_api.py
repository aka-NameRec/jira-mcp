from __future__ import annotations

import base64
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

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
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
        return response.json()

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
