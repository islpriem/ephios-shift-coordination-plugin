"""The event type says what kind of event it is and carries the assembly defaults."""

import pytest
from django.urls import reverse
from ephios.core.models import EventType

from ephios_shift_coordination.ephios_integration import (
    assembly_defaults,
    assembly_types,
    is_assembly,
    is_service,
)


@pytest.mark.django_db
def test_existing_event_types_are_neither_service_nor_assembly(planning_data):
    """Both marks are off by default, so nothing changes for types that already exist."""
    event_type = planning_data.template.event_type
    assert not is_service(event_type)
    assert not is_assembly(event_type)
    assert assembly_types() == []


@pytest.mark.django_db
def test_a_marked_type_offers_itself_for_assemblies_with_its_defaults(planning_data):
    event_type = EventType.objects.create(title="Team meeting", default_description="Agenda below")
    event_type.preferences["shift_coordination__is_assembly"] = True
    event_type.preferences["shift_coordination__assembly_title"] = "Team meeting A"
    event_type.preferences["shift_coordination__assembly_location"] = "Back room"
    event_type.preferences["visible_for"] = [planning_data.group]
    event_type.preferences["responsible_groups"] = [planning_data.coordination]

    assert is_assembly(event_type) and not is_service(event_type)
    assert assembly_types() == [event_type]
    defaults = assembly_defaults(event_type)
    assert defaults["title"] == "Team meeting A"
    assert defaults["location"] == "Back room"
    assert defaults["description"] == "Agenda below"
    assert list(defaults["visible_for"]) == [planning_data.group]
    assert list(defaults["responsible_groups"]) == [planning_data.coordination]


@pytest.mark.django_db
def test_the_native_event_type_page_offers_both_marks(client, planning_data):
    """The options live in the ephios event type editor, not in a second settings page."""
    client.force_login(planning_data.admin)
    page = client.get(
        reverse("core:settings_eventtype_edit", args=[planning_data.template.event_type.pk])
    )
    assert page.status_code == 200
    assert b"shift_coordination__is_service" in page.content
    assert b"shift_coordination__is_assembly" in page.content
