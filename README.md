# ephios shift coordination

An ephios plugin for availability surveys and recurring service planning.

The plugin targets ephios 0.27.0 and Python 3.14. The installation and development
scaffold is implemented; surveys, templates and scheduling are still in development.
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

`make check` is also the pre-commit and CI command. Its initial browser tests
log synthetic administrators in, check the plugin in ephios settings in English
and German, then verify activation and installation after a normal container
restart. The test stack is stopped afterwards; its local data is retained.
Python coverage currently covers the scaffold. There are no plugin templates
yet, so template coverage is not applicable. This is not full feature acceptance.

## Local stack

```sh
make dev-up
make dev-down
```

ephios is available at <http://127.0.0.1:8097> and captured mail at
<http://127.0.0.1:8098>. PostgreSQL listens on `127.0.0.1:5497`.
Override `EPHIOS_HTTP_PORT`, `EPHIOS_MAIL_PORT` and `EPHIOS_DATABASE_PORT` if
necessary. Data stays under `.local/data/test/`; `EPHIOS_STACK` selects another
workspace-local data directory and Compose project. Stopping the stack does not
delete data. The development stack does not create accounts automatically.

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
