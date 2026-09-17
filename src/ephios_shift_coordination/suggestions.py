"""Pure capacity estimate behind the recommended personal maximum."""

from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_array
from scipy.sparse.csgraph import maximum_flow


@dataclass(frozen=True)
class Suggestion:
    maximum: int
    places: int
    fillable: int
    shifts: int
    people: int

    @property
    def fills_all(self):
        return self.fillable == self.places


def fillable_places(demands, eligibility, cap):
    """Return how many minimum places can be filled if nobody takes more than `cap` shifts.

    Only qualifications matter: everybody is assumed to be available for every shift
    they may take. Scheduling rules and actual answers are deliberately ignored.
    """
    people = sorted(eligibility)
    shifts = sorted(demands)
    person_index = {pid: 1 + i for i, pid in enumerate(people)}
    shift_index = {sid: 1 + len(people) + i for i, sid in enumerate(shifts)}
    sink = 1 + len(people) + len(shifts)
    rows, cols, capacities = [], [], []
    for pid in people:
        rows.append(0)
        cols.append(person_index[pid])
        capacities.append(cap)
        for sid in sorted(eligibility[pid]):
            rows.append(person_index[pid])
            cols.append(shift_index[sid])
            capacities.append(1)
    for sid in shifts:
        rows.append(shift_index[sid])
        cols.append(sink)
        capacities.append(demands[sid])
    graph = csr_array(
        (np.asarray(capacities, dtype=np.int32), (rows, cols)), shape=(sink + 1, sink + 1)
    )
    return int(maximum_flow(graph, 0, sink).flow_value)


def suggest_maximum(demands, eligibility, maximum=None):
    """Smallest equal per-person cap that reaches the largest achievable staffing.

    `demands` maps shift IDs to their minimum staffing, `eligibility` maps person IDs
    to the shift IDs they are qualified for. A given `maximum` is not searched for but
    reported with the staffing it allows.
    """
    demands = {sid: count for sid, count in demands.items() if count > 0}
    eligibility = {
        pid: set(shifts) & demands.keys()
        for pid, shifts in eligibility.items()
        if set(shifts) & demands.keys()
    }
    places = sum(demands.values())
    if not eligibility or maximum == 0:
        return Suggestion(maximum or 0, places, 0, len(demands), len(eligibility))
    if maximum is not None:
        return Suggestion(
            maximum,
            places,
            fillable_places(demands, eligibility, maximum),
            len(demands),
            len(eligibility),
        )
    low, high = 1, max(len(shifts) for shifts in eligibility.values())
    best = fillable_places(demands, eligibility, high)
    while low < high:
        middle = (low + high) // 2
        if fillable_places(demands, eligibility, middle) >= best:
            high = middle
        else:
            low = middle + 1
    return Suggestion(low, places, best, len(demands), len(eligibility))
