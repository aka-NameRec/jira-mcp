"""Unit tests for the pure server helpers: field-alias translation and transition resolution."""

from __future__ import annotations

import pytest

from jira_mcp.field_mapping import FieldMapping
from jira_mcp.server import _resolve_transition_id, _translate_fields


def test_translate_aliases_and_passthrough() -> None:
    mapping = FieldMapping(acceptance_criteria="customfield_10", business_context=None, design_links=None)
    out = _translate_fields({"acceptance_criteria": "AC text", "summary": "S"}, mapping)
    assert out == {"customfield_10": "AC text", "summary": "S"}


def test_translate_missing_mapping_raises() -> None:
    mapping = FieldMapping(acceptance_criteria=None, business_context=None, design_links=None)
    with pytest.raises(ValueError, match="acceptance_criteria"):
        _translate_fields({"acceptance_criteria": "x"}, mapping)


def test_translate_empty_fields() -> None:
    mapping = FieldMapping(acceptance_criteria=None, business_context=None, design_links=None)
    assert _translate_fields({}, mapping) == {}


def test_resolve_transition_by_id() -> None:
    available = [{"id": "11", "name": "Start Progress"}, {"id": "21", "name": "Done"}]
    assert _resolve_transition_id("21", available) == "21"


def test_resolve_transition_by_name_case_insensitive() -> None:
    available = [{"id": "31", "name": "In Progress"}]
    assert _resolve_transition_id("in progress", available) == "31"


def test_resolve_transition_not_found_lists_available() -> None:
    available = [{"id": "1", "name": "Done"}]
    with pytest.raises(ValueError, match="Done"):
        _resolve_transition_id("Nope", available)
