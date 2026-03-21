from __future__ import annotations

import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, Field, ValidationError, field_validator


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "aka.NameRec@gmail.com" / "mcp" / "config.toml"


class ConfigError(RuntimeError):
    pass


class JiraFieldMappings(BaseModel):
    acceptance_criteria: str | None = None
    business_context: str | None = None
    design_links: str | None = None


class JiraProfile(BaseModel):
    base_url: AnyHttpUrl
    token: str
    email: str | None = None
    name: str | None = None
    deployment: str = "dc"
    auth_type: str = "bearer"
    issue_key_prefixes: list[str] = Field(default_factory=list)
    verify_tls: bool = True
    ca_bundle_path: str | None = None
    timeout_seconds: float = 30.0
    max_comments: int = 40
    max_comment_chars: int = 12000
    field_mappings: JiraFieldMappings = Field(default_factory=JiraFieldMappings)

    @field_validator("deployment")
    @classmethod
    def validate_deployment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"dc", "cloud"}:
            raise ValueError("deployment must be either 'dc' or 'cloud'.")
        return normalized

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"bearer", "basic"}:
            raise ValueError("auth_type must be either 'bearer' or 'basic'.")
        return normalized

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None

    @field_validator("issue_key_prefixes")
    @classmethod
    def normalize_prefixes(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value if item.strip()]
        if len(set(normalized)) != len(normalized):
            raise ValueError("issue_key_prefixes must be unique within a profile.")
        return normalized

    @property
    def normalized_base_url(self) -> str:
        return str(self.base_url).rstrip("/")

    @property
    def host(self) -> str:
        return urlsplit(self.normalized_base_url).netloc

    @property
    def resolved_name(self) -> str:
        return self.name or self.normalized_base_url


class JiraConfig(BaseModel):
    profiles: list[JiraProfile] = Field(default_factory=list)


class AppConfig(BaseModel):
    jira: JiraConfig = Field(default_factory=JiraConfig)


def get_config_path() -> Path:
    override = os.environ.get("AKA_MCP_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def _load_raw_config() -> dict:
    config_path = get_config_path()
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}. Create it and add a [jira] profiles list."
        )
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse config file {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {config_path} must contain a TOML table at the root.")
    return data


def load_jira_profiles() -> list[JiraProfile]:
    try:
        config = AppConfig.model_validate(_load_raw_config())
    except ValidationError as exc:
        raise ConfigError(f"Invalid Jira config: {exc}") from exc

    profiles = config.jira.profiles
    if not profiles:
        raise ConfigError(
            f"No Jira profiles configured in {get_config_path()}. Add entries under [jira]."
        )

    seen_names: dict[str, str] = {}
    seen_urls: dict[str, str] = {}
    seen_prefixes: dict[str, str] = {}
    for profile in profiles:
        resolved_name = profile.resolved_name
        if resolved_name in seen_names:
            raise ConfigError(
                f"Duplicate Jira profile name '{resolved_name}'. "
                "Profiles must have unique names after URL-based fallback."
            )
        seen_names[resolved_name] = profile.normalized_base_url

        if profile.normalized_base_url in seen_urls:
            raise ConfigError(
                f"Duplicate Jira base_url '{profile.normalized_base_url}'. "
                "Profile selection by URL would become ambiguous."
            )
        seen_urls[profile.normalized_base_url] = resolved_name

        for prefix in profile.issue_key_prefixes:
            if prefix in seen_prefixes:
                raise ConfigError(
                    f"Duplicate Jira issue key prefix '{prefix}' in profiles "
                    f"'{seen_prefixes[prefix]}' and '{resolved_name}'."
                )
            seen_prefixes[prefix] = resolved_name
    return profiles


def resolve_profile_for_url(url: str) -> JiraProfile:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError("Issue URL must be http or https.")
    host = parsed.netloc
    matches = [profile for profile in load_jira_profiles() if profile.host == host]
    if not matches:
        raise ConfigError(f"No Jira profile matched host '{host}'.")
    if len(matches) > 1:
        names = ", ".join(profile.resolved_name for profile in matches)
        raise ConfigError(
            f"Multiple Jira profiles match host '{host}': {names}. "
            "Check the config file for duplicate or overlapping entries."
        )
    return matches[0]


def resolve_profile_for_issue_key(issue_key: str) -> JiraProfile:
    normalized = issue_key.strip().upper()
    prefix = normalized.split("-", 1)[0]
    matches = [profile for profile in load_jira_profiles() if prefix in profile.issue_key_prefixes]
    if not matches:
        raise ConfigError(f"No Jira profile matched issue key prefix '{prefix}'.")
    if len(matches) > 1:
        names = ", ".join(profile.resolved_name for profile in matches)
        raise ConfigError(
            f"Multiple Jira profiles match issue key prefix '{prefix}': {names}. "
            "Check the config file for duplicate prefix assignments."
        )
    return matches[0]
