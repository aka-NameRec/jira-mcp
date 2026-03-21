from __future__ import annotations

from dataclasses import dataclass

from .config import JiraProfile


@dataclass(frozen=True)
class FieldMapping:
    acceptance_criteria: str | None
    business_context: str | None
    design_links: str | None


def build_field_mapping(profile: JiraProfile) -> FieldMapping:
    return FieldMapping(
        acceptance_criteria=profile.field_mappings.acceptance_criteria,
        business_context=profile.field_mappings.business_context,
        design_links=profile.field_mappings.design_links,
    )
