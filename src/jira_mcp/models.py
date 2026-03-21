from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ParsedIssueRef(BaseModel):
    url: str
    host: str
    profile_name: str
    base_url: str
    issue_key: str
    api_url: str


class IssueForReview(BaseModel):
    issue: dict[str, Any]
    requirements_text: str
    acceptance_criteria_text: str | None
    business_context_text: str | None
    implementation_notes_text: str | None
    comments_summary: dict[str, Any]
    comments: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    linked_work: dict[str, Any]
    source_fields: dict[str, Any]
