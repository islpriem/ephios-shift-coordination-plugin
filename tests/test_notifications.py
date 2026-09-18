"""Wording and content of the business messages."""

from datetime import timedelta

from django.utils import timezone
from ephios.core.models import Event, Shift
from ephios.core.models.users import Notification

from ephios_shift_coordination.assemblies import answer
from ephios_shift_coordination.models import Assembly
from ephios_shift_coordination.notifications import (
    AssemblyInvitation,
    PlanPublished,
    ShiftUnderstaffed,
    StaffingChanged,
    SurveyInvitation,
    SurveyReminder,
)
from ephios_shift_coordination.publication import publish_plan
from ephios_shift_coordination.staffing import add_person, remove_person
from ephios_shift_coordination.surveys import process_surveys
from tests.test_assemblies import call
from tests.test_observers import observer_data as observer_data
from tests.test_publication import prepare
from tests.test_staffing import member_with_access
from tests.test_staffing import published_data as published_data
from tests.test_surveys import open_for
from tests.test_surveys import survey_data as survey_data


def test_invitation_explains_the_purpose_and_the_recommendation(survey_data):
    data = survey_data
    period = open_for(data, maximum=2)
    notice = Notification.objects.get(user=data.member, slug=SurveyInvitation.slug)
    subject, body = str(notice.subject), str(notice.body)
    assert "Service planning" in subject and "help" in subject
    assert "Hello member" in body
    assert "not an assignment yet" in body and "personal maximum" in body
    assert "about 2 shifts per person" in body and "sit in" not in body
    assert f"/surveys/{period.pk}/" in notice.get_actions()[0][1]


def test_invitation_mentions_sitting_in_where_it_is_allowed(observer_data):
    notice = Notification.objects.filter(slug=SurveyInvitation.slug).first()
    assert "sit in on a service" in str(notice.body)


def test_reminder_names_the_missing_answer_and_the_deadline(survey_data, monkeypatch):
    data = survey_data
    period = open_for(data, reminder_days=[3])
    monkeypatch.setattr("django.utils.timezone.now", lambda: period.deadline - timedelta(days=2))
    process_surveys()
    notice = Notification.objects.filter(slug=SurveyReminder.slug).first()
    assert "still missing" in str(notice.subject)
    assert "still missing your availability" in str(notice.body)
    # The service name would only repeat itself: the period is the date range.
    assert "Duty" not in str(notice.subject)


def test_survey_messages_fall_back_when_the_answer_is_gone(survey_data):
    data = survey_data
    period = open_for(data)
    notice = Notification.objects.get(user=data.member, slug=SurveyInvitation.slug)
    reminder = Notification.objects.create(
        user=data.member,
        slug=SurveyReminder.slug,
        data={"period_id": period.pk, "dispatch_key": "due:x"},
    )
    period.responses.filter(user=data.member).delete()
    assert str(notice.subject) == str(SurveyInvitation.title)
    assert str(notice.body) == "" and notice.is_obsolete
    assert str(reminder.subject) == str(SurveyReminder.title)


def test_published_summary_lists_shifts_partners_and_sitting_in(observer_data):
    data = observer_data
    shift = data.shifts[0].pk
    publish_plan(
        data.coordinator,
        data.period.pk,
        **prepare(data, [[data.member.pk, shift]], observers=[[data.admin.pk, shift]]),
    )
    staffed = Notification.objects.get(user=data.member, slug=PlanPublished.slug)
    sitting = Notification.objects.get(user=data.admin, slug=PlanPublished.slug)
    body = str(staffed.body)
    assert "Hello member" in body and "published" in body
    assert "Shift 1" in body and "/events/" in body
    assert "admin" in body  # the partner sitting in is named
    assert "sitting in, no working hours" in str(sitting.body)
    assert str(staffed.subject).startswith("Service plan")
    assert "just been published" in body and "ephios calendar" in body
    # One button cannot stand for several services, so the text carries the links.
    assert not staffed.get_actions()
    data.period.delete()
    assert str(staffed.subject) == str(PlanPublished.title)
    assert str(staffed.body) == "" and not staffed.get_actions()


def test_the_understaffing_warning_points_to_the_replacement_overview(published_data):
    data = published_data
    remove_person(data.member, data.shifts[0].pk, user_id=data.member.pk)
    warning = Notification.objects.get(slug=ShiftUnderstaffed.slug)
    assert "no longer staffed" in str(warning.subject)
    assert "Shift 1" in str(warning.subject)
    body = str(warning.body)
    assert "member signed off" in body and "0 of at least 1" in body
    assert [label for label, _url in warning.get_actions()]
    # The frozen shift data keeps the message readable even after a native deletion.
    data.shifts[0].shift.delete()
    warning.refresh_from_db()
    assert "Shift 1" in str(warning.subject)
    assert str(warning.body) == "" and warning.is_obsolete and not warning.get_actions()
    data.period.delete()
    assert str(warning.subject) == str(ShiftUnderstaffed.title)


def test_staffing_changes_tell_the_person_what_happened(published_data):
    data = published_data
    guest = member_with_access(data, "guest")
    planned = data.period.events.last().shifts.order_by("pk").first()
    add_person(data.coordinator, planned.pk, user_id=guest.pk)
    assigned = Notification.objects.get(user=guest, slug=StaffingChanged.slug)
    assert "You are staffed for" in str(assigned.subject)
    assert "coordinator staffed you" in str(assigned.body)
    assert "sign off" in str(assigned.body)
    remove_person(data.coordinator, planned.pk, user_id=guest.pk)
    removed = Notification.objects.filter(user=guest, slug=StaffingChanged.slug).latest("pk")
    assert "no longer staffed" in str(removed.subject)
    assert "removed you from" in str(removed.body)
    assert assigned.is_obsolete and not removed.is_obsolete
    data.period.rules = {**data.period.rules, "allow_observers": True}
    data.period.save()
    add_person(data.coordinator, planned.pk, user_id=guest.pk, observer=True)
    sitting = Notification.objects.filter(user=guest, slug=StaffingChanged.slug).latest("pk")
    assert "no qualification is required" in str(sitting.body)
    planned.shift.delete()
    assert "Shift 1" in str(sitting.subject)
    assert str(sitting.body) == "" and sitting.is_obsolete
    data.period.delete()
    assert str(sitting.subject) == str(StaffingChanged.title)


def test_the_invitation_carries_the_agenda_the_own_status_and_one_link(
    planning_data, assembly_type
):
    assembly = call(planning_data, assembly_type, silent=False)
    notice = Notification.objects.get(user=planning_data.member, slug=AssemblyInvitation.slug)
    assert "not answered yet" in str(notice.body)
    label, link = notice.get_actions()[0]
    assert "Answer" in label and "/assemblies/answer/" in link
    answer(assembly, planning_data.member, attending=True)
    assert "said yes so far" in str(notice.body)
    answer(assembly, planning_data.member, attending=False)
    assert "said no so far" in str(notice.body)
    assert not notice.is_obsolete


def test_an_invitation_survives_the_assembly_being_deleted(planning_data, assembly_type):
    call(planning_data, assembly_type, silent=False)
    notice = Notification.objects.filter(
        user=planning_data.member, slug=AssemblyInvitation.slug
    ).first()
    Assembly.objects.all().delete()
    assert str(notice.subject) == str(AssemblyInvitation.title)
    assert str(notice.body) == "" and not notice.get_actions() and notice.is_obsolete


def test_an_invitation_is_obsolete_once_the_assembly_happened(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type, silent=False)
    notice = Notification.objects.filter(
        user=planning_data.member, slug=AssemblyInvitation.slug
    ).first()
    assert not notice.is_obsolete
    Shift.objects.filter(event=assembly.event).update(
        start_time=timezone.now() - timedelta(hours=3), end_time=timezone.now() - timedelta(hours=1)
    )
    assert notice.is_obsolete


def test_the_invitation_leaves_out_what_the_assembly_does_not_have(planning_data, assembly_type):
    assembly = call(planning_data, assembly_type, description="", agenda="", silent=False)
    Event.objects.filter(pk=assembly.event_id).update(location="")
    notice = Notification.objects.filter(
        user=planning_data.member, slug=AssemblyInvitation.slug
    ).first()
    body = str(notice.body)
    assert "Where:" not in body and "Agenda:" not in body
    assert "you are invited to Monthly meeting" in body
    # Without a shift there is no time to name, and the invitation stays readable.
    Shift.objects.filter(event=assembly.event).delete()
    assert "Monthly meeting" in str(notice.subject) and "Monthly meeting" in str(notice.body)
