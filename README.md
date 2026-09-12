# ephios shift coordination

An ephios plugin for availability surveys and recurring service planning.

The plugin targets ephios 0.27.0 and Python 3.14. Planning permissions, settings,
service templates and recurring event creation are implemented. Surveys and
assignment planning are still in development.
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
selection, event creation and member access. Installation and activation are
checked again after a normal container restart. The test stack is stopped
afterwards; its local data is retained. Reports are under `.local/test-results/`.
Template rendering coverage is reported separately at file level; it does not
measure template branches. Full survey and assignment acceptance is still pending.

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
not passed in command arguments. Importing does not send invitations or mail.

The dataset contains:

- 100 members, `demo-001@example.invalid` through `demo-100@example.invalid`,
  with the initial password `demo-only-member-password`.
- Planning permissions for the first two members; the other 98 are ordinary
  members. Demo qualifications are split between both shifts, with 20 people
  qualified for both.
- A `Dienst` event type and service template with `Schicht 1` (09:00–13:00) and
  `Schicht 2` (13:00–17:00), meeting 15 minutes earlier, minimum two and maximum
  three people each. Each shift requires its corresponding demo qualification.

Repeating the import reuses existing members and the `Dienst` template. It does
not reset existing passwords or overwrite template settings. No events or survey
responses are generated. Holiday exclusion stays disabled until a region is
configured. These example values are only created by this explicit demo command.

For an unattended import without the administrator prompt:

```sh
scripts/run uv run --locked python scripts/project.py import-demo --no-admin
```

Log in at <http://127.0.0.1:8097>. Administrators manage planning groups, defaults
and service templates through ephios settings. Coordinators use **Dienstplanung**
to select a template and period, calculate the initial dates, adjust individual
days, inspect the preview and create native events. Events are immediately
visible according to their permissions and self-signup is disabled. The creator
is also made responsible, in addition to the template's configured responsibles.

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
