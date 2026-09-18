# ephios shift coordination

An ephios plugin for availability surveys and recurring service planning.

The plugin targets ephios 0.27.0 and Python 3.14. Planning permissions, settings,
service templates, recurring event creation, availability surveys, shared planning
drafts, automatic proposals and publication through native participations are implemented.
Members can offer to sit in on a service without taking a shift, coordinators can close
a survey early, and after publication everybody staffed can sign off or step in through
the replacement overview. Event types can additionally be marked as assemblies, which are
called with one short form and answered straight from the invitation mail.
It is based on the [official plugin template](https://github.com/ephios-dev/ephios-plugin-template).

## Development

All project environments, downloads, caches and test data stay in this checkout.
Use `scripts/run` when invoking development tools so they use the local environment paths.
The existing Docker runtime manages its own image and container storage.

Prerequisites: Git, Python 3.14, uv 0.12.8 and Docker with Compose. On Linux,
Chromium's system libraries must already be available. CI uses Ubuntu 24.04.
No global packages or Docker settings are changed by the setup.

```sh
make setup       # Locked local environment, Chromium and repository Git hook
make test        # Python tests and branch-enabled coverage (minimum 95%)
make format      # Ruff formatting and automatic lint fixes
make check       # Formatting, lint, tests, coverage, migrations and browser tests
make build       # Wheel, source archive and selected container build inputs
```

`make check` is also the pre-commit and CI command. It includes template linting,
PostgreSQL concurrency tests and browser tests for setup, template editing, date
selection, event creation, private surveys, deadlines, shared drafts, recorded exceptions,
proposal preview/adoption, publication, captured summary mail, the existing personal
calendar feed and later native reassignment. Installation and activation are
checked again after a normal container restart. The test stack is stopped
afterwards; its local data is retained. Reports are under `.local/test-results/`.
Template rendering coverage is reported separately at file level; it does not
measure template branches.

Optimizer checks include exhaustive small cases, invalid solver results and timeouts,
plus fixed-seed loads of 100 people and about 200 shifts over three months. Separate
realistic and scarce cases record model/stage timings and validated results. Browser
load reports also record the full HTTP time, snapshot query count and database/runtime
versions. Pure calculations run under a subprocess watchdog; HTTP tests have an outer
timeout and exercise the calendar during calculation. Allowing people to sit in makes the
model larger, because every service a candidate could serve offers its other shifts as well;
those columns carry no capacity and no neighbouring-day row of their own, so the load case
stays inside its budget.

## Local stack

```sh
make dev-up
make dev-down
```

ephios is available at <http://127.0.0.1:8097> and captured mail at
<http://127.0.0.1:8098>. PostgreSQL listens on `127.0.0.1:5497`.
Override `EPHIOS_HTTP_PORT`, `EPHIOS_MAIL_PORT` and `EPHIOS_DATABASE_PORT` if
necessary. Data stays under `.local/data/development/`; `EPHIOS_STACK` selects another
workspace-local data directory and Compose project. Stopping the stack does not
delete data. The development stack does not create accounts automatically.

Automated tests use `.local/data/test/` and separate ports: 8099 for ephios, 8100
for Mailpit and 5499 for PostgreSQL. These can be changed with
`EPHIOS_TEST_HTTP_PORT`, `EPHIOS_TEST_MAIL_PORT` and `EPHIOS_TEST_DATABASE_PORT`.
Keep the `test` stack name reserved for automated checks.

## Demo data and a local administrator

```sh
make dev-import-demo
```

This starts the development stack, imports synthetic data and then runs the
native ephios administrator creation prompt. Enter the administrator's email,
display name, date of birth and password there. Password input is hidden and is
not passed in command arguments.

The dataset contains:

- 30 members, `demo-001@example.invalid` through `demo-030@example.invalid`,
  with the initial password `demo`.
- Planning permissions for the first two members; the others are ordinary
  members. Demo qualifications are split between both shifts, with every fifth
  person qualified for both.
- A `Dienst` event type and service template with `Schicht 1` (09:00–13:00) and
  `Schicht 2` (13:00–17:00), meeting 15 minutes earlier, minimum two and maximum
  three people each. Each shift requires its corresponding demo qualification.
- One called assembly in about three weeks, invited, with an agenda and about half the
  answers in, plus reminder rules of one day before services and three and one day before
  assemblies.
- Four fortnightly planning periods with seven services each, one per state you
  may want to try: a finished published one in the past, a published one that is
  about to start, one whose survey is closed and that is waiting to be planned
  (its last two services deliberately lack people), and one whose survey is still
  running with about half of the answers in. Answers cover the whole rating
  scale, some people offer to sit in and some leave a note.

Repeating the import reuses existing members, the `Dienst` template and the
existing periods. It does not reset passwords, overwrite template settings or
create a second set of periods. Holiday exclusion stays disabled until a region
is configured. These example values are only created by this explicit demo command.

For an unattended import without the administrator prompt, and to start from an
empty database:

```sh
scripts/run uv run --locked python scripts/project.py import-demo --no-admin
scripts/run uv run --locked python scripts/project.py import-demo --reset
```

`--reset` deletes the development stack's database, uploads and captured mail
under `.local/data/development/` before importing. It never touches another stack.

Log in at <http://127.0.0.1:8097>. Administrators manage planning groups, defaults
and service templates through ephios settings. The holiday region, the calculation
budget and the number of regular people a shift needs besides anybody sitting in are
instance-wide settings; periods only carry the values that differ per period.

Coordinators use **Dienstplanung** and create a period in three steps: template and
date range, the single dates in the calendar, then a summary of the services that will
be created. Events are immediately visible according to their permissions and
self-signup is disabled. The creator is also made responsible, in addition to the
template's configured responsibles.

On the period page, set the response deadline, the reminder offsets and the recommended
number of shifts per person, then open the survey. The recommendation is calculated from
the shifts, their minimum staffing and the qualifications of the invited members: it is
the smallest number of shifts per person that still allows every shift to be staffed.
Members see that number as the default for their personal maximum together with a short
explanation. The deadline must precede the first shift.

Members use **Umfragen** and answer in one compact table: one row per service, one column
group per shift they may take, four colored choices per shift and, where the period allows
it, one checkbox to offer sitting in. They set a personal maximum (including zero) and can
add a private note. They can replace the complete response until the deadline; afterwards
it remains readable. Coordinators see the answers on the period page; qualification changes
are shown without changing the original questions or saved ratings.

Sitting in means joining a service without taking a shift: no qualification is required,
the time earns no working hours, and the person still counts towards the staffing of the
shift. Every shift keeps at least the configured number of regularly staffed people, so a
shift is never staffed by people sitting in alone. When a period allows it, everybody who
can see the services is invited to the survey, even without a matching qualification.

Whoever already serves a shift that day may sit in on the other shifts of the same service
without offering it separately: they are on site anyway, so it needs no extra offer and does
not use up another of their services. Only sitting in on a day somebody would otherwise stay
at home counts against their personal maximum and needs their offer.

Invitations and reminders use ephios notification preferences and its normal
`run_periodic` command. Only unanswered surveys receive reminders; repeated runs
do not create duplicate dispatches. After an outage, only the newest due reminder
is created. The server locks answers at the deadline even if the periodic command
has not yet run. Opening a survey does not create shift participations.

Coordinators use **Dienste planen** to select people in a calendar that shows the whole
period at once and only the weekdays that actually carry shifts. Each shift card lists the
people already selected and hides the remaining candidates behind one click. Counts update
immediately; the server checks the current qualifications, responses, personal and weekly
limits, consecutive days, overlaps and shift capacity. Only confirmed services of the same
event type affect these checks. A speech-bubble icon marks everybody who wrote a note, both
in the sidebar and on their assignments, and the counter above the calendar filters the
sidebar down to them. Notes are visible to coordinators and are never passed to the
independent scheduling module.

Planning is possible while the survey is still running, which makes an interim result
visible; new answers make a saved draft outdated, and publishing requires a closed survey.
**Umfrage jetzt schließen** ends an open survey early after an explicit confirmation:
answers become read-only and no further reminders are sent.

A red exclamation mark marks every assignment that breaks a rule, with the reason in its
tooltip; shift-wide problems such as a partly staffed shift sit in the shift's own header.
One banner lists all of them as bullet points and is confirmed once, with an optional common
note, before **Gemeinsamen Entwurf speichern**. A shift that has people but fewer than its
minimum is such a rule violation: that service cannot take place, so either fill it or leave
it empty. Saving replaces the shared draft and records every exception separately without
changing responses, creating native participations or sending email. Other coordinators see it after loading.
Stale versions or changed native inputs require reloading; deleted or inaccessible targets
cannot be overridden. Recorded reasons remain available after subsequent draft edits.

**Vorschlag berechnen** creates a complete alternative from current responses and native
commitments. It first maximizes fully staffed shifts, then, where sitting in is possible,
uses as few people sitting in as it can, then minimizes assignments marked "if needed" and
finally maximizes strong preferences. Each proposed shift has exactly its minimum staffing
or stays empty. A final bounded heuristic reduces repeated pairs without changing those
scores or breaking a rule. Existing draft selections and manual exceptions are
not optimizer inputs. The pure Python entry point is `optimizer.propose_plan(PlanningInput)`;
its data contract contains IDs, times, limits and ratings, with no names or notes.

The preview reports whether all three priority objectives were proven optimal or the
time limit left a validated intermediate result. A valid empty proposal is shown explicitly.
Calculation changes no saved assignments. **Vorschlag übernehmen** replaces the local
selection, with confirmation when there are unsaved edits; save the shared draft separately.
Failed calculations keep the current selections. Changed permissions, versions or native
inputs require a fresh check before a proposal can be used or saved.

The default calculation budget is 10 seconds, shared by model preparation, all solver
stages, partner improvement and final validation. Database reads and HTTP overhead are
measured separately. Solver time limits are cooperative; the budget is not a hard real-time
guarantee. Configure application and proxy request timeouts above the chosen budget plus
database overhead when increasing it. Calculation releases database locks, then rechecks
the input version and fingerprint before returning the result.

Use **Gespeicherten Entwurf zur Veröffentlichung prüfen** to review assignments,
summary recipients, recorded exceptions and missing minimum places. Confirm every
exception and any understaffing, then publish the saved plan. The server rechecks
permissions, the saved version and current data inside the publication transaction.
Changed inputs require checking and saving the shared draft again before publication.

Publication creates confirmed native ephios participations, retains native logging,
and records the published assignments, author, time and exceptions. A failed database
write rolls back the whole operation. Repeating the same publication returns its
existing record without creating more participations or summary notifications.
Each assigned person receives one summary through their ephios notification settings;
delivery starts after commit. Summaries respect current partner visibility and contain
no survey notes or exception reasons. A delivery failure leaves the plan published and
the notification available to ephios's existing delivery mechanism.

After publication every service page links to **Besetzung und Nachbesetzung**. Coordinators
and everybody staffed that day see who answered that they are available, whether that person
could take the shift regularly or by sitting in, their answer on the four-step scale and what
would speak against it, for example a service on the neighbouring day, a reached weekly limit
or a reached personal maximum. Members sign off there or on the service page with **Ich bin
verhindert**, take a free place with **Ich übernehme die Schicht** or offer **Ich sitze bei**.
Coordinators can staff a candidate directly from the list, and the affected person is told.
When signing off leaves a previously staffed shift below its minimum, every responsible
coordinator gets a message that points to the replacement overview. The page is not visible
to members who are neither staffed that day nor coordinating.

People sitting in join as native placeholder participations: they count towards the staffing
of the shift in ephios and receive the plugin's messages, but they earn no working hours and
the shift does not appear in their personal ephios calendar.

## Assemblies

An event type is marked in its ephios editor as a service, as an assembly, or as neither.
Both marks default to off, so existing event types are unaffected. An assembly type also
carries the default title and location of its assemblies; description, invited groups and
responsibles are the event type's own ephios fields.

Whoever is responsible for such a type uses **Versammlungen** and calls one with a single
form: kind, title, location, description, date, times and an optional agenda. The page lists
who would be invited before anything is sent. Calling an assembly creates one ordinary ephios
event with one shift that uses instant confirmation, needs no qualification and has no minimum
staffing, so everybody invited answers for themselves and may change their mind later.

The invitation names the time, the place, the agenda and the person's current answer, and
carries a signed link. That link opens a page with one yes and one no button without a login;
nothing is written when the link is merely opened, so mail scanners and link previews cannot
answer for anybody. Answers go through the native signup logic and appear in ephios like any
other participation. **Still einberufen** notifies nobody, and the assembly page offers the
invitation later, together with the date it was last sent. Sending it again is deliberate,
for example after the agenda changed.

## Reminders

Planning settings carry two reminder rules: one for services and one for assemblies. Each is a
list of day offsets and one time of day, so "1, 0" with 09:00 reminds at nine on the day before
and again on the day itself. An empty list means no reminder, which is the default.

Service reminders reach everybody confirmed for the shift, including the people sitting in, and
name the others as far as ephios shows them to that person. Assembly reminders reach everybody
invited whatever they answered, and repeat the agenda, the current own answer and the same
signed link. Reminders ride the normal `run_periodic` command: each one goes out once per person
and appointment, repeated runs create nothing, and a reminder whose moment passed during an
outage is recorded rather than sent late. The assembly page shows the responsibles which
reminders went out and when the next one is due.

Members use their existing personal ICS URL in ephios calendar settings. The plugin
creates no ICS files or separate feed: confirmed participations automatically appear
in the native calendar. Make later assignment changes through ephios. The plugin keeps
the original publication record, marks differences and links to current shifts; native
calendar subscriptions follow those subsequent changes.

The demo loader runs only in the development configuration with the internal
mail catcher. It is excluded from the plugin distribution and image build inputs.

The image uses the official ephios 0.27.0 image pinned by digest and installs the
plugin wheel through `uv add`, updating the image's project and lockfile together.
The official image reports its source version as `0.0.0`; the derived image sets
that package metadata to `0.27.0` and narrows its Python requirement to 3.14.
The normal ephios entrypoint runs migrations and builds static and language files.
On Apple Silicon, this upstream image runs as `linux/amd64` through Docker's emulation.

The Docker build context contains only the wheel and Dockerfile. No development
source checkout is mounted in the application container. All service images are
pinned by digest. This Compose configuration is exclusively for local testing;
production installation, updates and recovery are not yet validated.

Never use real member data or production mail credentials for local tests.
