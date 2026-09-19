# Development

Everything this project downloads, builds or runs stays inside the checkout. No global
packages are installed and no Docker settings are changed.

## Prerequisites

Git, Python 3.14, uv 0.12.8 or newer, and Docker with Compose. On Linux, Chromium's system libraries
have to be present. CI runs on Ubuntu 24.04.

Use `scripts/run` in front of development tools so they use the local environment paths.

```sh
make setup       # locked environment, Chromium and the repository git hook
make test        # Python tests with branch coverage (minimum 95 %)
make format      # formatting and automatic lint fixes
make check       # everything CI runs, and the pre-commit hook
make build       # wheel, source archive and the container build context
```

## What `make check` runs

Ruff, the formatter, template linting, the Python tests with coverage, the migration check,
and then the browser suite: it builds the wheel, starts an isolated Compose stack, seeds demo
and test data, runs the PostgreSQL concurrency tests and the Chromium tests, and finally
checks that the plugin is still installed and enabled after a normal container restart.
Reports land in `.local/test-results/`.

The optimizer has its own checks: exhaustive small cases, invalid solver results, timeouts,
and a fixed-seed load of 100 people and about 200 shifts over three months. Pure calculations
run under a subprocess watchdog; the HTTP tests have an outer timeout.

## Local stack

```sh
make dev-up      # ephios at http://127.0.0.1:8097, captured mail at http://127.0.0.1:8098
make dev-down
```

PostgreSQL listens on `127.0.0.1:5497`. Data stays under `.local/data/development/`; stopping
the stack keeps it. `EPHIOS_HTTP_PORT`, `EPHIOS_MAIL_PORT` and `EPHIOS_DATABASE_PORT` override
the ports, `EPHIOS_STACK` selects another workspace-local data directory and Compose project.

The automated tests use the `test` stack on ports 8099, 8100 and 5499. Keep that name for them.

## Demo data

```sh
make dev-import-demo    # starts the stack, imports synthetic data, then asks for an admin
make dev-reset-demo     # the same, but deletes the development stack's data first
```

Thirty members `demo-001@example.invalid` … `demo-030@example.invalid` with the password
`demo`, a `Dienst` template with two shifts, four fortnightly planning periods covering every
state worth trying, and one called assembly with an agenda and half the answers in. Repeating
the import reuses what is there instead of creating a second set.

The demo loader refuses to run outside the development configuration with its internal mail
catcher, and it is excluded from the distribution and the image build inputs.

## Layout

| Path | What lives there |
| --- | --- |
| `src/ephios_shift_coordination/` | the plugin: models, views, forms, templates, notifications |
| `src/ephios_shift_coordination/scheduling.py`, `optimizer.py` | the rules and the solver, no Django import |
| `tests/` | Python tests; `tests/e2e/` holds the browser suite |
| `deploy/` | Dockerfile, Compose file and nginx configuration of the local stack |
| `scripts/` | project commands and the demo loader |

Never use real member data or production mail credentials locally.
