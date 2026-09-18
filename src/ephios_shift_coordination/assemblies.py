"""Calling an assembly: one native event with a single shift everybody invited can answer.

Assemblies reuse the ordinary ephios event, shift and participation machinery. The plugin
only adds the agenda, the invitation and a signed link so people can answer from the mail.
"""

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _
from ephios.core.forms.events import EventForm
from ephios.core.models import AbstractParticipation, LocalParticipation, Shift, UserProfile
from guardian.shortcuts import get_objects_for_user, get_users_with_perms

from .access import enabled
from .ephios_integration import assembly_defaults, assembly_types, is_assembly
from .models import Assembly
from .services import Conflict

ANSWER_SALT = "ephios_shift_coordination.assembly_answer"
# A link is meant for one assembly, and assemblies are called a few months ahead at most.
ANSWER_MAX_AGE = 365 * 24 * 60 * 60


def can_call(user, event_type):
    """Whoever is responsible for an assembly type may call its assemblies."""
    if not enabled() or not user.is_authenticated or not user.is_active:
        return False
    if not is_assembly(event_type) or not user.has_perm("core.add_event"):
        return False
    if user.has_perm("core.change_event"):  # whoever manages ephios is responsible everywhere
        return True
    defaults = assembly_defaults(event_type)
    groups = set(user.groups.values_list("pk", flat=True))
    return bool(groups & {group.pk for group in defaults["responsible_groups"]}) or any(
        person.pk == user.pk for person in defaults["responsible_users"]
    )


def callable_types(user):
    return [event_type for event_type in assembly_types() if can_call(user, event_type)]


def would_invite(user, event_type):
    """Who an assembly of this type would reach, shown to the caller before anything is sent."""
    defaults = assembly_defaults(event_type)
    groups = [
        *visible_groups(user, defaults),
        *(group.pk for group in defaults["responsible_groups"]),
    ]
    responsibles = {user.pk, *(person.pk for person in defaults["responsible_users"])}
    return people(
        UserProfile.objects.filter(Q(groups__in=groups) | Q(pk__in=responsibles)),
    )


def invited(event):
    """Everybody who may see the assembly, which is exactly who is invited to it."""
    return people(
        get_users_with_perms(event, only_with_perms_in=["view_event"], with_group_users=True)
    )


def people(queryset):
    return queryset.filter(is_active=True).order_by("display_name", "pk").distinct()


def shift_of(assembly):
    return assembly.event.shifts.first()


def own_state(assembly, user):
    """The answer this person has given so far, or None if they have not answered."""
    shift = shift_of(assembly)
    if shift is None or not user.is_authenticated:
        return None
    participation = LocalParticipation.objects.filter(shift=shift, user=user).first()
    return participation.state if participation else None


def answer_state(assembly, user):
    """The answer as two plain flags, so templates never compare native state numbers."""
    state = own_state(assembly, user)
    return {
        "yes": state == AbstractParticipation.States.CONFIRMED,
        "no": state == AbstractParticipation.States.USER_DECLINED,
    }


def visible_groups(user, defaults):
    """The type's groups, limited to those this coordinator may publish events for."""
    allowed = get_objects_for_user(user, "publish_event_for_group", klass=Group)
    groups = allowed.filter(pk__in=[group.pk for group in defaults["visible_for"]]) or allowed
    if not groups:
        raise PermissionDenied(_("You cannot publish events for any group."))
    return list(groups.values_list("pk", flat=True))


@transaction.atomic
def plan_assembly(user, *, event_type, title, description, location, start, end, agenda, silent):
    """Create the event and its single shift, and invite unless the assembly is called quietly."""
    if not can_call(user, event_type):
        raise PermissionDenied
    if end <= start:
        raise ValidationError(_("The assembly has to end after it starts."))
    defaults = assembly_defaults(event_type)
    form = EventForm(
        user=user,
        eventtype=event_type,
        data={
            "title": title,
            "description": description,
            "location": location,
            "visible_for": visible_groups(user, defaults),
            "responsible_groups": [group.pk for group in defaults["responsible_groups"]],
            "responsible_users": sorted(
                {user.pk, *(person.pk for person in defaults["responsible_users"])}
            ),
        },
    )
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    event = form.save()
    shift = Shift(
        event=event,
        label=_("Assembly"),
        meeting_time=start,
        start_time=start,
        end_time=end,
        # Everybody invited answers for themselves and may change their mind, and an
        # assembly asks for no qualification, so nobody is turned away by the structure.
        signup_flow_slug="instant_confirmation",
        signup_flow_configuration={
            "signup_until": None,
            "user_can_decline_confirmed": True,
            "user_can_customize_signup_times": False,
        },
        structure_slug="uniform",
        structure_configuration={
            "required_qualification_ids": [],
            "minimum_number_of_participants": 0,
            "maximum_number_of_participants": None,
        },
    )
    shift.full_clean()
    shift.save()
    event.activate()
    assembly = Assembly.objects.create(event=event, agenda=agenda, created_by=user)
    if not silent:
        invite(user, assembly)
    return assembly


def invite(user, assembly):
    """Ask everybody invited to answer. Sending again later is allowed and deliberate."""
    from ephios.core.services.notifications.backends import send_all_notifications

    from .notifications import AssemblyInvitation

    if not user.has_perm("core.change_event", assembly.event):
        raise PermissionDenied
    recipients = list(invited(assembly.event))
    AssemblyInvitation.send(assembly, recipients)
    assembly.invited_at = timezone.now()
    assembly.save(update_fields=["invited_at"])
    # An invitation people only see on their next visit is not an invitation.
    transaction.on_commit(lambda: send_all_notifications(), robust=True)
    return recipients


def answer_link(assembly, user):
    """A signed link that identifies one person for one assembly, without a login."""
    return dumps({"assembly": assembly.pk, "user": user.pk}, salt=ANSWER_SALT)


def resolve_answer(token):
    try:
        payload = loads(token, salt=ANSWER_SALT, max_age=ANSWER_MAX_AGE)
    except SignatureExpired as exc:
        raise Conflict(_("This link has expired. Please open the assembly in ephios.")) from exc
    except BadSignature as exc:
        raise PermissionDenied from exc
    assembly = Assembly.objects.filter(pk=payload["assembly"]).select_related("event").first()
    user = UserProfile.objects.filter(pk=payload["user"], is_active=True).first()
    if assembly is None or user is None or not user.has_perm("core.view_event", assembly.event):
        raise PermissionDenied
    return assembly, user


def answer(assembly, user, *, attending):
    """Write the answer through the native signup flow, so every native check still runs."""
    shift = shift_of(assembly)
    if shift is None:
        raise Conflict(_("This assembly has no shift any more."))
    if shift.end_time <= timezone.now():
        raise Conflict(_("This assembly is over."))
    participant = user.as_participant()
    validator = shift.signup_flow.get_validator(participant)
    state = own_state(assembly, user)
    if attending:
        if state == AbstractParticipation.States.CONFIRMED:
            return participant.participation_for(shift)
        if errors := validator.get_signup_errors():
            raise Conflict(" ".join(str(error) for error in errors))
        return shift.signup_flow.perform_signup(participant, acting_user=user)
    if state == AbstractParticipation.States.USER_DECLINED:
        return participant.participation_for(shift)
    if errors := validator.get_decline_errors():
        raise Conflict(" ".join(str(error) for error in errors))
    return shift.signup_flow.perform_decline(participant, acting_user=user)
