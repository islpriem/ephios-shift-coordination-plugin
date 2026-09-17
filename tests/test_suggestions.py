"""The recommended personal maximum and the capacity estimate behind it."""

from ephios_shift_coordination.suggestions import fillable_places, suggest_maximum
from ephios_shift_coordination.surveys import capacity_preview, recommendation
from tests.test_surveys import open_for
from tests.test_surveys import survey_data as survey_data


def cohort(people, shifts, both=0):
    """Split people between two qualification groups, `both` of them can do either shift."""
    eligibility = {}
    for index in range(people):
        if index < both:
            eligibility[index + 1] = set(shifts)
        else:
            eligibility[index + 1] = {sid for sid in shifts if sid % 2 == index % 2}
    return eligibility


def test_recommendation_counts_qualifications_not_just_places():
    demands = dict.fromkeys(range(1, 61), 2)
    suggestion = suggest_maximum(demands, cohort(100, demands, both=20))
    assert suggestion.places == 120 and suggestion.people == 100
    # 120 places for 100 people is 1.2 on average, but the qualification split needs two.
    assert suggestion.maximum == 2 and suggestion.fills_all
    assert fillable_places(demands, cohort(100, demands, both=20), 1) < 120


def test_recommendation_is_honest_when_the_shifts_cannot_be_filled():
    suggestion = suggest_maximum({1: 2, 2: 2}, {1: {1}, 2: {1}, 3: {2}})
    assert suggestion.maximum == 1 and not suggestion.fills_all
    assert suggestion.fillable == 3 and suggestion.places == 4


def test_a_given_maximum_is_reported_with_the_staffing_it_allows():
    demands = {1: 2, 2: 2}
    eligibility = {1: {1, 2}, 2: {1, 2}}
    assert suggest_maximum(demands, eligibility).maximum == 2
    assert suggest_maximum(demands, eligibility, maximum=1).fillable == 2
    assert suggest_maximum(demands, eligibility, maximum=0).fillable == 0
    assert suggest_maximum({}, {}).maximum == 0


def test_capacity_preview_and_frozen_recommendation(survey_data):
    data = survey_data
    preview = capacity_preview(data.period)
    # Two people hold the qualification of the first shift, nobody the second one.
    assert preview["people"] == 2 and preview["shifts"] == 4
    assert "shifts" in recommendation(preview)
    period = open_for(data, maximum=3)
    assert period.suggestion["maximum"] == 3
    assert period.suggestion["places"] == preview["places"]
    assert recommendation(period.suggestion)


def test_the_recommendation_prefills_the_personal_maximum(survey_data, client):
    from ephios_shift_coordination.forms import SurveyResponseForm

    data = survey_data
    period = open_for(data, maximum=2)
    response = period.responses.get(user=data.member)
    assert SurveyResponseForm(response=response).initial["maximum"] == 2
    client.force_login(data.member)
    page = client.get(response.get_absolute_url())
    assert b'value="2"' in page.content


def test_the_recommendation_says_when_every_shift_can_be_staffed():
    filled = recommendation({"maximum": 2, "places": 4, "fillable": 4, "shifts": 2})
    assert "all 2" in filled and "2 shifts on average" in filled
    assert not recommendation({})
