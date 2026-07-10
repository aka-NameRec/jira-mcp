# jira-mcp

Lightweight MCP server for Jira-backed review and repository research workflows.

This project is intentionally narrow. It is designed for agents such as Codex, Cursor, and similar MCP-capable tools that need compact access to:
- Jira issues used as implementation and review context;
- issue comments that contain requirement clarifications;
- linked work and attachment metadata relevant to code review;
- future write operations such as posting issue comments back to Jira.

The goal is not broad Jira administration. The goal is to give an agent just enough context to review code, investigate the codebase, and search for implementation decisions grounded in the actual issue discussion.

## Design principle — self-describing, low-friction tools

**A client should be able to use this server correctly on the first try with no external docs.**
An MCP agent only sees each tool's name, signature, and docstring, plus the server
`instructions` string — so those must carry everything needed to use the tool safely and
intuitively. This README is background for humans, not a prerequisite for the agent.

Every tool must therefore:
- have a docstring that states **what it does, when to use it, whether it reads or mutates**,
  the meaning and format of each non-obvious argument, and the key fields of the result;
- surface sharp edges **inline** where they bite (e.g. user search is Latin-only; `log_work`
  has its own endpoint and is not a settable field; transitions are effectively one-way);
- return **self-explanatory results** that tell the agent what to do next — e.g. `find_issues`
  returns `status: "assignee_not_found" | "assignee_ambiguous"` with a `hint` and `candidates`
  instead of guessing, and `search_users` returns the exact `assignee` value `assign_issue`
  expects, so multi-step flows (resolve → confirm → assign) are discoverable without a manual.

The server `instructions` string is the map: it groups read vs write tools and spells out the
cross-tool flows. When you add or change a tool, updating its docstring and the `instructions`
string is part of the change — if an agent would have to read this README to use the tool, the
docstring is incomplete.

## Tooling Scope

Read-only:
- `parse_issue_url` — parse a Jira URL into a stable issue reference.
- `get_issue_for_review` — compact issue + comments + attachments + linked work.
- `list_transitions` — workflow transitions available from the issue's current status.
- `my_issues` — issues assigned to the current user across all configured profiles.
- `find_issues` — search issues by flexible filters, incl. another person by name (resolved, no id needed); reports not-found / ambiguous instead of guessing.
- `search_users` — find users by name / username / email; returns the `assignee` value to pass to `assign_issue`.
- `list_worklogs` — worklog entries (logged time) on an issue, plus total seconds.
- `whoami` — the current user per profile (use for assignee identity).
- `list_issue_types` — issue types available for creating issues in a project.
- `get_create_metadata` — fields (and which are required) for creating a given issue type.
- `list_link_types` — issue link types with their outward/inward phrases (for `link_issues`).
- `list_priorities` — priorities available on the instance (names are instance-specific).
- `find_sprints` — a project's scrum-board sprints (any state); reports `no_board` / `no_sprints` and asks instead of guessing when several match.

Write (mutate real Jira — use with explicit intent):
- `update_issue` — set fields; `add` / `remove` change multi-value fields via Jira's update verb without clobbering.
- `add_comment`, `delete_comment` (destructive), `transition_issue` (by id/name, optional comment), `create_issue`.
- `log_work` — log time on an issue (Jira duration like `3h`, `1h 30m`); its own endpoint, not a settable field.
- `assign_issue` / `unassign_issue` — assign to a user by the `assignee` value from `search_users`, or clear it.
- `link_issues` — link two issues ("outward `<type>` inward", e.g. `Blocks`); accepts a type name or a directional phrase (an inward phrase auto-swaps the two issues).
- `set_sprint` — move an issue into a sprint by its numeric id (from `find_sprints`).

## Usage & safety

Write tools change real Jira issues. Use them only with explicit user intent and confirm before destructive or hard-to-reverse actions.

- **Read-modify-write for multi-value fields.** `update_issue` SETS fields and replaces multi-value fields (`labels`, `components`, `fixVersions`) and `description`. To change multi-value fields without clobbering, use `add` / `remove` (Jira update verb); each value may be a scalar or a list.
- **Transitions.** State- and permission-dependent and often one-way. Call `list_transitions` first; reverting a status can take several hops. `transition_issue`'s optional `comment` is posted as a separate comment, so a transition screen without a comment field never silently drops it.
- **Sub-tasks cannot nest.** Create a sub-task with a sub-task issue type and `fields={"parent": {"key": "<PARENT>"}}` under a standard (non-sub-task) issue.
- **Assignee by name, no id needed.** Resolve a person with `search_users` (matches username / display name / email), confirm the right match with the user, then `assign_issue` with the returned `assignee` value; `unassign_issue` clears it. Display names are stored in Latin script, so a Cyrillic query returns nothing — transliterate (`Омуркулов` → `Omurkulov`), and a first+last name disambiguates common surnames. `assign_issue` uses the dedicated `PUT .../assignee` endpoint (DC `name`, Cloud `accountId`), separate from `update_issue`.
- **Find another person's issues.** `find_issues(assignee="Aidin Omurkulov", status_category="in progress")` resolves the name and lists their issues; `status='assignee_not_found'` / `'assignee_ambiguous'` (with candidates, no issues) mean *ask the user*, don't guess. `status_category` accepts friendly EN/RU synonyms (`in progress` / `в процессе` → `In Progress`); `order_by` is whitelisted against JQL injection.
- **Worklog / logged time.** `log_work` posts to `POST .../worklog` (`timeSpent` in Jira duration syntax; `started` defaults to now if omitted; `adjust_estimate` is `auto` or `leave`). It is NOT settable through `update_issue`. `list_worklogs` reviews existing entries. There is no worklog-delete tool — remove a mistaken entry in the Jira UI.
- **Estimate vs deadline vs logged time (three different things).** A bare duration like `2h` is an *original estimate* — `update_issue`/`create_issue` `fields={"timetracking": {"originalEstimate": "2h"}}`. A calendar *deadline* is `fields={"duedate": "YYYY-MM-DD"}`. Time *already spent* is `log_work`. If the user just says "set 2h", ask whether that's the estimate or a due date.
- **Linking issues.** `link_issues(outward_issue, inward_issue, link_type)` reads "outward `<type>` inward". Pass a `link_type` name or a directional phrase from `list_link_types`; naming the inward phrase swaps the two issues so the sentence still holds. Both issues must be on the same instance.
- **Sprints (Jira Software).** `find_sprints(project)` lists candidate sprints from the project's scrum boards (kanban boards have none); if several match, it asks which rather than guessing. `set_sprint(issue, sprint_id)` uses the Agile endpoint, so no Sprint field id is needed. **Sub-tasks follow their parent's sprint** — `set_sprint` on a sub-task returns success but does not move it; sprint the parent instead. Priority names for `{"priority": {"name": ...}}` come from `list_priorities` (this instance has both `High` and `Highest`).
- **Irreversibility.** `delete_comment` is permanent; `create_issue` has no delete (cancel instead).
- **`notify_users=false`** requires Jira admin rights (403 otherwise) — leave the default.
- **Field aliases.** `update_issue` / `create_issue` accept `acceptance_criteria` / `business_context` / `design_links`, mapped to the profile's `field_mappings` customfield ids.
- **create_issue project vs profile.** `project_or_prefix` is used both as the literal project key and to select the profile via its configured `issue_key_prefixes`; it breaks if a project's key differs from its prefix.
- **Cloud caveat.** Live-tested on Data Center (API v2). On Cloud (v3) these paths are DC-only until a Cloud branch is added: `my_issues` uses the deprecated `GET /search`; `list_issue_types` / `get_create_metadata` use the classic `GET /issue/createmeta` (removed on Cloud) and return empty there; ADF wrapping covers `description` but not rich-text customfields.

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
