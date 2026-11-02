# Scheduling rules and concurrent services

The planner maximises the number of **individual shifts that reach their minimum staffing**.
Sitting in can contribute to that minimum. Every staffed shift must still have its configured
minimum number of qualified regular people, capped at the shift's minimum staffing.

## Concurrent telephone and chat shifts

One person may take a regular duty and sit in on one or more other shifts of the **same
ephios event**, including shifts that run simultaneously or start after midnight. Two
overlapping regular duties remain incompatible. The exception also permits simultaneous
observer roles within one event. It does not extend to another event just because its date
or event type is the same.

For example, telephone and chat run from 18:00 to 22:00, each requiring two people and at
least one regular person. Alice and Bob can both take telephone duty; Bob can also take chat
duty. Both strongly prefer telephone. With sitting in enabled, the following plan staffs
both shifts:

| Person | Telephone | Chat | Services counted towards the personal limit |
| --- | --- | --- | --- |
| Alice | Regular duty | Sitting in | 1 |
| Bob | Sitting in | Regular duty | 1 |

This is better than assigning both people to telephone and leaving chat empty. The number
of staffed shifts takes precedence over every preference and over minimising sitting in.
If either shift requires two regular people, the example cannot staff both shifts with
only these two people. Disabling sitting in also rules out this arrangement.

## Availability, qualifications and limits

- Red means **unavailable for that shift**, including sitting in. An observer offer or
  a regular duty on a sibling shift does not override red.
- Automatic regular assignments require a complete response, a positive rating and the
  required qualifications valid through the shift. Missing answers are not availability.
- Automatic sitting in requires a complete response and either an explicit offer or a
  regular duty at that same event. Sitting in requires no qualification.
- Sitting in beside an own regular duty at the same event adds no load to personal or
  weekly limits. This also holds when the sibling shift starts in the next calendar week.
- Without an own regular duty at that event, each observer assignment uses one service
  from those limits. A personal limit of zero prevents these assignments.
- Existing confirmed regular and observer participations of the same event type are
  included. Their individual participation times, where set, determine conflicts and dates.
- Overlap and free-next-day checks apply between events of the same type. All commitments
  of other event types are ignored, as required by this plugin's scheduling policy.

The database adapters always supply native event identifiers. The pure Python contract
also accepts older callers without event identifiers; such inputs never receive the
concurrent-role exception. Its legacy date-based load grouping remains available to those
callers. Native commitments are context, not assignments the solver is allowed to move.
The production planning adapter refuses already occupied planned shifts; moving published
participations belongs to the replacement workflow.

## Ordered optimisation

The solver uses separate MILP runs with previously proven objective values fixed:

1. Maximise shifts fully staffed to their minimum.
2. Minimise the number of observer assignments.
3. Minimise regular assignments rated “if needed”.
4. Maximise regular assignments rated “particularly wanted”.

The second priority is the existing observer policy, introduced before this correction.
It does not trade away a staffed shift. There is no weighted sum that lets enough strong
preferences outweigh an additional staffed shift. Ratings in priorities 3 and 4 concern
regular duty only. Red independently excludes both roles.

Each suggested shift receives exactly its minimum number of people, or zero. Extra places
and partial staffing are left for manual decisions. Binary role constraints prevent the
same person appearing twice in one shift. Personal and weekly capacities charge regular
duties and standalone observer roles; conditional credits remove the load of observers
beside an own regular duty. Time conflicts distinguish regular and observer roles and use
the event boundary described above.

Finally, a bounded local search tries to reduce repeated pairs without worsening any of
the preceding criteria. It changes regular assignments, keeps observer assignments fixed,
and independently checks every accepted alternative. Pair variety is heuristic; it is not
globally proven optimal, especially where improving it would require changing observer
roles too.

The configured budget defaults to ten seconds and covers model construction, solver stages
and pair improvements, with a small reserve for validation. Solver time limits are not a
hard operating-system deadline. The load tests additionally use a process watchdog.

| Result | Meaning |
| --- | --- |
| `optimal_primary` | Every applicable MILP priority above is proven optimal for the supplied inputs. Pair variety remains heuristic. |
| `feasible_timeout` | The returned plan passes independent rule validation, but some priority remains unproven. |
| `no_proposal` | No validated proposal is returned; the current draft is preserved. This does not prove infeasibility. |
| `invalid_input` | The pure input contract is inconsistent; source data must be reloaded or corrected. |

## Publication and later role changes

Draft validation and publication use the same role semantics. Before publishing, current
users, shifts, qualifications and existing participations are read again; relevant rows
are locked. Regular duties become native local participations. Observers become native
placeholder participations linked to their member, without working hours. Existing
notification, visibility and calendar integration remains in use.

After publication, use the plugin's **Besetzung und Nachbesetzung** page. To move a regular
duty from telephone to chat, leave the telephone role, take chat duty and join telephone as
an observer. The last two actions may happen in either order. These are separate actions,
so a temporary staffing gap can generate the existing coordinator notification. A one-click
atomic transfer is not part of this change.

Self-service is an explicit new decision: it does not require the old survey to offer the
new role. Capacity, qualifications for regular duty, event visibility, activity, shift start
and role-specific overlap checks still apply. The replacement list suggests eligible
observer roles when an existing sibling duty prevents taking another regular duty.
The published snapshot remains a record of the original plan.

Before publication, the planner's person selector can add a person or change an existing
selection between regular duty and sitting in. A role change is an unsaved change and is
protected by the confirmation before replacing the draft with a proposal. The preview
keeps the solver's optimality or timeout explanation visible alongside its staffing totals.

## Regression audit, October 2026

The reported failure was reproduced with two simultaneous shifts and two members. The old
MILP disallowed every overlap regardless of role, so the desired two-shift solution was
outside its feasible set. Reporting the one-shift solution as optimal was correct for that
incorrect model. The same unconditional check blocked the self-service buttons.

The correction also addresses related gaps: already published observers were missing from
future planning inputs; replacement suggestions omitted sibling commitments; observer
conflicts were not consistently scoped by event type; browser counters charged free
observer roles as additional duties. Existing observers now affect stale-draft detection
and publication locking as well as scheduling.

Verification is encoded in the repository:

- [Independent exhaustive role oracle](../tests/test_concurrent_observers.py): enumerates
  absent, regular and observer roles without using the solver or production validator;
  compares all four objectives on 36 deterministic varied cases and targeted examples.
- [Publication regression](../tests/test_parallel_publication.py): real surveys, proposal,
  save, publication, native participations and subsequent HTTP self-service.
- [Staffing tests](../tests/test_staffing.py): role transfer, multiple sibling observers,
  red answers, other event types and individual participation times.
- [Database concurrency tests](../tests/test_concurrency.py): stale edits, duplicate
  publication and changes to native regular/observer records during publication.
- [Chromium workflow](../tests/e2e/test_staffing.py): simultaneous shifts, personal limit
  one, displayed counters, publication, observer sign-off and rejoining through the button.
- [Load tests](../tests/test_optimizer_performance.py): 100 people, 200 shifts over three
  months, including concurrent shifts with normal and scarce staffing. The browser load
  test also exercises 201 concurrent shifts through the real proposal endpoint.

These tests use synthetic data and ephios 0.27. They do not replay a production database or
certify interactions with a separate plugin that is not installed in the test environment.

## Updating an existing installation

No database migration is required for this correction. Existing published plans are not
rewritten. Open planning pages should be reloaded and saved drafts reviewed and saved again:
the richer input fingerprint deliberately invalidates the previous calculation basis.
Recorded rule confirmations may need renewal when their underlying facts have changed.
