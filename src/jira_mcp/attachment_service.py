from __future__ import annotations

from .jira_api import JiraAdapter


def normalize_attachment(attachment: dict, client: JiraAdapter) -> dict:
    return {
        "id": attachment.get("id"),
        "filename": attachment.get("filename"),
        "mime_type": attachment.get("mimeType"),
        "size": attachment.get("size"),
        "created": attachment.get("created"),
        "author": {
            "name": (attachment.get("author") or {}).get("displayName"),
            "email": (attachment.get("author") or {}).get("emailAddress"),
        },
        "content_url": client.make_absolute_url(attachment.get("content")),
        "thumbnail_url": client.make_absolute_url(attachment.get("thumbnail")),
    }
