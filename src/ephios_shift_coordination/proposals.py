"""Short transactional input reads around a calculation without database locks."""

from dataclasses import asdict
from time import monotonic

from django.db import transaction
from django.utils.translation import gettext as _

from .drafts import check_basis, snapshot
from .optimizer import Status, propose_plan


def create_proposal(user, period_id, *, expected_version, fingerprint):
    started = monotonic()
    with transaction.atomic():
        initial = snapshot(user, period_id)
        check_basis(initial, expected_version, fingerprint)
    input_seconds = monotonic() - started
    result = propose_plan(initial.data)
    recheck_start = monotonic()
    with transaction.atomic():
        current = snapshot(user, period_id)
        check_basis(current, expected_version, fingerprint)
    output = asdict(result)
    output.update(version=expected_version, fingerprint=fingerprint)
    output["diagnostics"].update(
        input_seconds=input_seconds,
        recheck_seconds=monotonic() - recheck_start,
        request_seconds=monotonic() - started,
    )
    output["message"] = {
        Status.OPTIMAL_PRIMARY: _(
            "Full staffing, yellow assignments and strong preferences are optimized. "
            "Partner variety is a heuristic improvement."
        ),
        Status.FEASIBLE_TIMEOUT: _(
            "The time limit was reached. This proposal follows all rules, "
            "but not all priority objectives are proven optimal."
        ),
        Status.NO_PROPOSAL: _(
            "No validated proposal is available. Your current draft has been kept. "
            "A time limit does not prove that no suitable plan exists."
        ),
        Status.INVALID_INPUT: _(
            "The planning inputs are inconsistent. Reload and check the source data."
        ),
    }[result.status]
    return output
