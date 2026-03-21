from __future__ import annotations

from typing import Any

from .attachment_service import normalize_attachment
from .comment_normalizer import html_to_text, normalize_comment
from .config import JiraProfile
from .field_mapping import FieldMapping
from .jira_api import JiraAdapter


def _extract_field_text(
    fields: dict[str, Any], rendered_fields: dict[str, Any], field_id: str | None
) -> str | None:
    if not field_id:
        return None
    rendered_value = rendered_fields.get(field_id)
    if isinstance(rendered_value, str):
        text = html_to_text(rendered_value)
        if text:
            return text
    raw_value = fields.get(field_id)
    if isinstance(raw_value, str) and raw_value.strip():
        return raw_value.strip()
    return None


def _compact_join(parts: list[str]) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def normalize_issue_for_review(
    raw_issue: dict[str, Any], profile: JiraProfile, field_mapping: FieldMapping, client: JiraAdapter
) -> dict[str, Any]:
    fields = raw_issue.get("fields", {})
    rendered_fields = raw_issue.get("renderedFields", {})
    names = raw_issue.get("names", {})

    description_text = html_to_text(rendered_fields.get("description")) or (
        fields.get("description", "") or ""
    ).strip()
    acceptance_criteria_text = _extract_field_text(
        fields, rendered_fields, field_mapping.acceptance_criteria
    )
    business_context_text = _extract_field_text(fields, rendered_fields, field_mapping.business_context)
    design_links_text = _extract_field_text(fields, rendered_fields, field_mapping.design_links)

    raw_comments = ((fields.get("comment") or {}).get("comments")) or []
    rendered_comment_items = (((rendered_fields.get("comment") or {}).get("comments")) or [])
    selected_comments = raw_comments[-profile.max_comments :]
    rendered_selected = rendered_comment_items[-profile.max_comments :]
    comment_limit = profile.max_comment_chars
    if len(rendered_selected) < len(selected_comments):
        rendered_selected = ([{}] * (len(selected_comments) - len(rendered_selected))) + rendered_selected
    comments = [
        normalize_comment(
            comment,
            rendered_selected[idx].get("body"),
            comment_limit,
        )
        for idx, comment in enumerate(selected_comments)
    ]

    requirement_comments = [item["body_text"] for item in comments if item["contains_requirement_signal"]]
    implementation_notes_text = _compact_join(requirement_comments) or None

    requirements_text = _compact_join(
        [
            fields.get("summary", ""),
            description_text,
            acceptance_criteria_text or "",
            business_context_text or "",
            design_links_text or "",
            implementation_notes_text or "",
        ]
    )

    attachments = [normalize_attachment(item, client) for item in fields.get("attachment", [])]
    subtasks = [
        {
            "key": subtask.get("key"),
            "summary": ((subtask.get("fields") or {}).get("summary")),
            "status": (((subtask.get("fields") or {}).get("status")) or {}).get("name"),
        }
        for subtask in fields.get("subtasks", [])
    ]
    issue_links = []
    for link in fields.get("issuelinks", []):
        inward = link.get("inwardIssue")
        outward = link.get("outwardIssue")
        linked_issue = inward or outward or {}
        link_type = link.get("type") or {}
        issue_links.append(
            {
                "direction": "inward" if inward else "outward" if outward else "unknown",
                "relationship": (
                    link_type.get("inward") if inward else link_type.get("outward") if outward else None
                ),
                "key": linked_issue.get("key"),
                "summary": ((linked_issue.get("fields") or {}).get("summary")),
                "status": (((linked_issue.get("fields") or {}).get("status")) or {}).get("name"),
            }
        )

    source_fields = {
        "acceptance_criteria": {
            "field_id": field_mapping.acceptance_criteria,
            "field_name": names.get(field_mapping.acceptance_criteria) if field_mapping.acceptance_criteria else None,
        },
        "business_context": {
            "field_id": field_mapping.business_context,
            "field_name": names.get(field_mapping.business_context) if field_mapping.business_context else None,
        },
        "design_links": {
            "field_id": field_mapping.design_links,
            "field_name": names.get(field_mapping.design_links) if field_mapping.design_links else None,
        },
    }

    return {
        "issue": {
            "key": raw_issue.get("key"),
            "id": raw_issue.get("id"),
            "summary": fields.get("summary"),
            "issue_type": ((fields.get("issuetype") or {}).get("name")),
            "status": ((fields.get("status") or {}).get("name")),
            "priority": ((fields.get("priority") or {}).get("name")),
            "assignee": ((fields.get("assignee") or {}).get("displayName")),
            "reporter": ((fields.get("reporter") or {}).get("displayName")),
            "labels": fields.get("labels", []),
            "components": [item.get("name") for item in fields.get("components", [])],
            "fix_versions": [item.get("name") for item in fields.get("fixVersions", [])],
            "url": f"{profile.normalized_base_url}/browse/{raw_issue.get('key')}",
        },
        "requirements_text": requirements_text,
        "acceptance_criteria_text": acceptance_criteria_text,
        "business_context_text": business_context_text,
        "implementation_notes_text": implementation_notes_text,
        "comments_summary": {
            "total_comments": len(raw_comments),
            "returned_comments": len(comments),
            "comments_with_requirement_signal": sum(
                1 for item in comments if item["contains_requirement_signal"]
            ),
        },
        "comments": comments,
        "attachments": attachments,
        "linked_work": {
            "subtasks": subtasks,
            "issue_links": issue_links,
        },
        "source_fields": source_fields,
    }
