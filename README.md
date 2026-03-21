# jira-mcp

Focused MCP server for Jira issue review workflows across multiple Jira profiles.

Current tools:
- `parse_issue_url`
- `get_issue_for_review`

## Why this server exists

This project is intentionally narrow. It targets workflows where Codex needs Jira issue requirements and clarifications to assess or propose implementation work in code review.

The server is read-only in the first version, but the internals are split so future capabilities can be added for:
- selective attachment downloads and extraction;
- posting comments back to Jira.
It also uses provider-aware adapters so Jira Data Center and Jira Cloud can evolve independently behind the same MCP tool contract.

## Review Context Policy

`get_issue_for_review` always includes:
- core issue fields;
- comments, because they may contain requirement clarifications;
- attachment metadata;
- linked work such as subtasks and issue links.

It assembles a compact review-oriented payload instead of returning raw Jira JSON.

## Requirements

- Python 3.13+
- `uv`
- A Jira token accepted by the target Jira deployment
- Network access to your Jira instance(s)

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

Typical use:
- self-hosted Jira / Jira Data Center
- bearer token or another deployment-specific token accepted by the instance
- short keys like `BL-123` resolve via `issue_key_prefixes`

Jira Cloud example:

```toml
[jira]
profiles = [
  { name = "intprop-cloud", base_url = "https://intprop.atlassian.net", email = "user@example.com", token = "classic-atlassian-api-token", deployment = "cloud", auth_type = "basic", issue_key_prefixes = ["ALS", "ORD"] }
]
```

Typical use:
- Atlassian Cloud tenant
- classic Atlassian API token
- `basic` auth built from `email + token`
- tenant REST API path like `https://<tenant>.atlassian.net/rest/api/3/...`

Profile fields:
- `base_url` required and unique
- `token` required
- `email` required for `auth_type = "basic"`
- `name` optional; defaults to `base_url`
- `deployment` required in practice: `dc` or `cloud`
- `auth_type` optional: `bearer` or `basic`
- `issue_key_prefixes` optional but required if you want short issue keys like `BL-123`
- `verify_tls` optional: defaults to `true`
- `ca_bundle_path` optional
- `timeout_seconds` optional
- `max_comments` optional
- `max_comment_chars` optional
- `field_mappings` optional

Cloud vs Data Center auth:
- `deployment = "dc"` usually works with a token the self-hosted instance accepts directly; `bearer` is common in our current setup.
- `deployment = "cloud"` currently works in this project with a classic Atlassian API token plus `auth_type = "basic"` and `email`.
- Scoped Atlassian Cloud tokens and bearer auth were not adopted in this project because the working path we validated is classic `basic` auth against the tenant REST API.

## Local Run

```bash
uv run jira-mcp
```

## Add To Codex

```bash
codex mcp add jira-review -- uv --directory /home/shtirliz/workspace/myself/experiments/jira-mcp run jira-mcp
```

Then restart Codex and confirm the server is visible:

```bash
codex mcp get jira-review --json
```

## Notes

- The server resolves the target profile by issue URL host or, for short issue keys, by configured `issue_key_prefixes`.
- Configuration errors are raised as user-facing tool errors with actionable messages.
- Comments are always included in the assembled review context.
- Attachments are currently returned as metadata only.
- Future attachment download and issue comment write tools can be added without restructuring the project.
