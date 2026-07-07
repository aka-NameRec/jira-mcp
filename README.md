# jira-mcp

Lightweight MCP server for Jira-backed review and repository research workflows.

This project is intentionally narrow. It is designed for agents such as Codex, Cursor, and similar MCP-capable tools that need compact access to:
- Jira issues used as implementation and review context;
- issue comments that contain requirement clarifications;
- linked work and attachment metadata relevant to code review;
- future write operations such as posting issue comments back to Jira.

The goal is not broad Jira administration. The goal is to give an agent just enough context to review code, investigate the codebase, and search for implementation decisions grounded in the actual issue discussion.

## Tooling Scope

Read-only:
- `parse_issue_url` — parse a Jira URL into a stable issue reference.
- `get_issue_for_review` — compact issue + comments + attachments + linked work.
- `list_transitions` — workflow transitions available from the issue's current status.
- `my_issues` — issues assigned to the current user across all configured profiles.

Write (mutate real Jira — use with explicit intent):
- `update_issue` — set fields; `add` / `remove` change multi-value fields via Jira's update verb without clobbering.
- `add_comment`, `delete_comment` (destructive), `transition_issue` (by id/name, optional comment), `create_issue`.

## Usage & safety

Write tools change real Jira issues. Use them only with explicit user intent and confirm before destructive or hard-to-reverse actions.

- **Read-modify-write for multi-value fields.** `update_issue` SETS fields and replaces multi-value fields (`labels`, `components`, `fixVersions`) and `description`. To change multi-value fields without clobbering, use `add` / `remove` (Jira update verb); each value may be a scalar or a list.
- **Transitions.** State- and permission-dependent and often one-way. Call `list_transitions` first; reverting a status can take several hops. `transition_issue`'s optional `comment` is posted as a separate comment, so a transition screen without a comment field never silently drops it.
- **Sub-tasks cannot nest.** Create a sub-task with a sub-task issue type and `fields={"parent": {"key": "<PARENT>"}}` under a standard (non-sub-task) issue.
- **Assignee.** Data Center: `fields={"assignee": {"name": "<username>"}}` (often the email). Cloud: `accountId`.
- **Irreversibility.** `delete_comment` is permanent; `create_issue` has no delete (cancel instead).
- **`notify_users=false`** requires Jira admin rights (403 otherwise) — leave the default.
- **Field aliases.** `update_issue` / `create_issue` accept `acceptance_criteria` / `business_context` / `design_links`, mapped to the profile's `field_mappings` customfield ids.
- **Cloud caveat.** Live-tested on Data Center (API v2). On Cloud (v3), `my_issues` uses the deprecated `GET /search` and ADF wrapping covers `description` but not rich-text customfields.

## Why This Exists

`jira-mcp` stays lightweight on purpose:
- focused on code review and implementation research instead of full Jira management;
- comments are included because real requirements often live there;
- payloads are normalized for agent use instead of exposing raw Jira JSON;
- the internal structure leaves room for comment-writing support without changing the public direction of the project.

This makes the server useful when an agent needs to read a task, inspect comments, correlate them with local code, and prepare or later publish review feedback.

## Review Context Policy

`get_issue_for_review` always includes:
- core issue fields;
- comments;
- attachment metadata;
- linked work such as subtasks and issue links.

It assembles a compact review-oriented payload instead of returning raw Jira JSON.

## Requirements

- Python 3.13+
- `uv`
- a Jira token accepted by the target Jira deployment
- network access to your Jira instance

## Version

- Current version: `0.1`
- Release date: `2026-03-26`

The CLI also exposes version output:

```bash
uv run jira-mcp --version
uv run jira-mcp -v
```

## Configuration

The server reads profiles from:

```bash
~/.config/aka.NameRec@gmail.com/mcp/config.toml
```

You can override the location for testing with:

```bash
export AKA_MCP_CONFIG_PATH="/path/to/config.toml"
```

Combined example:

```toml
[jira]
profiles = [
  { base_url = "https://jira.example.corp", token = "your-token", deployment = "dc", issue_key_prefixes = ["BL", "MKT"] },
  { name = "atlassian-cloud", base_url = "https://example.atlassian.net", email = "user@example.com", token = "cloud-token", deployment = "cloud", auth_type = "basic", issue_key_prefixes = ["OPS"], field_mappings = { acceptance_criteria = "customfield_12345" } }
]
```

Jira Data Center example:

```toml
[jira]
profiles = [
  { base_url = "https://jira.example.corp", token = "dc-token", deployment = "dc", auth_type = "bearer", issue_key_prefixes = ["BL", "MKT"], field_mappings = { acceptance_criteria = "customfield_12345" } }
]
```

Jira Cloud example:

```toml
[jira]
profiles = [
  { name = "intprop-cloud", base_url = "https://intprop.atlassian.net", email = "user@example.com", token = "classic-atlassian-api-token", deployment = "cloud", auth_type = "basic", issue_key_prefixes = ["ALS", "ORD"] }
]
```

Profile fields:
- `base_url` required and unique
- `token` required
- `email` required for `auth_type = "basic"`
- `name` optional; defaults to `base_url`
- `deployment` required in practice: `dc` or `cloud`
- `auth_type` optional: `bearer` or `basic`
- `issue_key_prefixes` optional but required if you want short issue keys such as `BL-123`
- `verify_tls` optional; defaults to `true`
- `ca_bundle_path` optional
- `timeout_seconds` optional
- `max_comments` optional
- `max_comment_chars` optional
- `field_mappings` optional

## Local Run

```bash
uv run jira-mcp
```

The server uses stdio transport by default, which is the expected transport for a local MCP server in agent environments.

## Tests

```bash
uv run --with pytest pytest
```

Mock-based suite (httpx `MockTransport`) covering request shapes, field-alias translation, transition resolution, `my_issues` merge, and error paths.

## Install From GitHub URL

Repository URL:

```text
git@github.com:aka-NameRec/jira-mcp.git
```

Minimal local install:

```bash
git clone git@github.com:aka-NameRec/jira-mcp.git
cd jira-mcp
uv sync
```

After that, register the server in your agent client using the repository directory as the working directory.

Codex example:

```bash
codex mcp add jira-review -- uv --directory /absolute/path/to/jira-mcp run jira-mcp
```

Cursor example:

```json
{
  "mcpServers": {
    "jira-review": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/jira-mcp",
        "run",
        "jira-mcp"
      ]
    }
  }
}
```

## Intended Agent UX

This repository is meant to be easy to install from a GitHub URL. A higher-level installer or agent can implement a flow like:

1. Clone the repository from GitHub.
2. Run `uv sync` in the cloned directory.
3. Register the MCP server with:

```bash
uv --directory /absolute/path/to/jira-mcp run jira-mcp
```

That makes requests such as `install MCP server git@github.com:aka-NameRec/jira-mcp.git` straightforward to automate in Codex, Cursor, or similar agents.

## Notes

- The server resolves the target profile by issue URL host or, for short issue keys, by configured `issue_key_prefixes`.
- Configuration errors are raised as user-facing tool errors with actionable messages.
- Comments are always included in the assembled review context.
- Attachments are currently returned as metadata only.
- Future issue comment write support can be added without changing the review-oriented direction of the project.
