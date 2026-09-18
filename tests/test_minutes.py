"""Filing, finding and opening assembly minutes."""

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from ephios_shift_coordination.minutes import display_name, file_minutes, remove, visible
from ephios_shift_coordination.models import AssemblyMinutes

from .test_assemblies import call

NAMESPACE = "ephios_shift_coordination:"


def url(name, *args):
    return reverse(NAMESPACE + name, args=args)


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    """Uploads belong in the test's own directory, never in the workspace media root."""
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def pdf(name="Protokoll aus dem Downloads-Ordner.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 minutes", content_type="application/pdf")


@pytest.mark.django_db
def test_the_upload_name_is_replaced_by_a_generated_one(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    minutes = file_minutes(planning_data.coordinator, assembly, pdf())
    assert minutes.file.name.startswith("assembly-minutes/")
    assert minutes.file.name.endswith(".pdf")
    assert "Downloads" not in minutes.file.name
    assert display_name(minutes).endswith(".pdf")
    assert minutes.uploaded_by == planning_data.coordinator


@pytest.mark.django_db
def test_only_a_responsible_may_file_or_delete_minutes(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    with pytest.raises(PermissionDenied):
        file_minutes(planning_data.member, assembly, pdf())
    minutes = file_minutes(planning_data.coordinator, assembly, pdf())
    with pytest.raises(PermissionDenied):
        remove(planning_data.member, minutes)
    remove(planning_data.coordinator, minutes)
    assert not AssemblyMinutes.objects.exists()


@pytest.mark.django_db
def test_minutes_are_only_listed_for_assemblies_somebody_may_see(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    file_minutes(planning_data.coordinator, assembly, pdf())
    assert visible(planning_data.member).count() == 1
    assert visible(planning_data.outsider).count() == 0


@pytest.mark.django_db
def test_the_search_matches_title_kind_agenda_and_date(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    file_minutes(planning_data.coordinator, assembly, pdf())
    day = assembly.event.shifts.first().start_time
    for term in ("Monthly", "Team meeting", "Duty plan", str(day.year), "nothing here"):
        found = visible(planning_data.member, term).count()
        assert found == (0 if term == "nothing here" else 1), term
    assert visible(planning_data.member, day.strftime("%d.%m.%Y")).count() == 1
    assert visible(planning_data.member, "Monthly Welcome").count() == 1


@pytest.mark.django_db
def test_the_file_opens_in_the_browser_instead_of_downloading(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    minutes = file_minutes(planning_data.coordinator, assembly, pdf())
    client.force_login(planning_data.member)
    response = client.get(url("minutes_file", minutes.pk))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("inline;")
    client.force_login(planning_data.outsider)
    assert client.get(url("minutes_file", minutes.pk)).status_code == 403


@pytest.mark.django_db
def test_the_pages_file_find_and_delete_minutes(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    client.force_login(planning_data.coordinator)
    assert client.post(url("minutes_upload", assembly.pk), {"file": pdf()}).status_code == 302
    minutes = AssemblyMinutes.objects.get()
    page = client.get(url("minutes_list"), {"q": "Monthly"}).content.decode()
    assert "Monthly meeting" in page
    assert (
        client.get(url("minutes_list"), {"q": "Nothing"}).content.decode().count("list-group-item")
        == 1
    )
    assert client.post(url("minutes_delete", minutes.pk)).status_code == 302
    assert not AssemblyMinutes.objects.exists()


@pytest.mark.django_db
def test_something_that_is_not_a_pdf_is_refused(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    client.force_login(planning_data.coordinator)
    upload = SimpleUploadedFile("notes.txt", b"not a pdf", content_type="text/plain")
    response = client.post(url("minutes_upload", assembly.pk), {"file": upload}, follow=True)
    assert not AssemblyMinutes.objects.exists()
    assert "pdf" in response.content.decode().lower()


@pytest.mark.django_db
def test_somebody_else_cannot_file_minutes_through_the_page(planning_data, client, assembly_type):
    assembly = call(planning_data, assembly_type)
    client.force_login(planning_data.member)
    assert client.post(url("minutes_upload", assembly.pk), {"file": pdf()}).status_code == 403


@pytest.mark.django_db
def test_the_assembly_page_offers_the_minutes_to_the_responsible(
    planning_data, client, assembly_type
):
    assembly = call(planning_data, assembly_type)
    file_minutes(planning_data.coordinator, assembly, pdf())
    client.force_login(planning_data.coordinator)
    page = client.get(assembly.event.get_absolute_url()).content.decode()
    assert "File the assembly minutes as PDF" in page and "Delete" in page
    client.force_login(planning_data.member)
    page = client.get(assembly.event.get_absolute_url()).content.decode()
    assert "Assembly minutes" in page and "File the assembly minutes as PDF" not in page


@pytest.mark.django_db
def test_an_iso_date_finds_the_minutes_as_well(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type)
    file_minutes(planning_data.coordinator, assembly, pdf())
    day = assembly.event.shifts.first().start_time.date()
    assert visible(planning_data.member, day.isoformat()).count() == 1


@pytest.mark.django_db
def test_an_upload_that_would_fill_the_disk_is_refused(
    planning_data, client, assembly_type, settings
):
    settings.GET_USERCONTENT_QUOTA = lambda: (0, 1)
    assembly = call(planning_data, assembly_type)
    client.force_login(planning_data.coordinator)
    response = client.post(url("minutes_upload", assembly.pk), {"file": pdf()}, follow=True)
    assert not AssemblyMinutes.objects.exists()
    assert "too large" in response.content.decode()
