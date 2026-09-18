# Releases

A release is a version bump. Change `version` in `pyproject.toml` on `main`, and the
[release workflow](../.github/workflows/release.yml) does the rest:

1. It reads the version and stops right there if the tag `v<version>` already exists, so
   ordinary pushes to `main` cost nothing.
2. It runs the full `make check`. Nothing is published unless that is green.
3. It builds the wheel and the source archive, builds the container image from the same wheel,
   and pushes it to `ghcr.io/islpriem/ephios-shift-coordination-plugin` as `<version>` and
   `latest`.
4. It creates the annotated tag `v<version>` and a GitHub release with generated notes and the
   two build artefacts attached.

Two jobs, one concurrency group, so two pushes in a row cannot race each other into half a
release.

## Why the version file and not a tag

Tag-driven releases are the more common pattern: you push `v1.2.3` and CI reacts. Watching the
version file instead means the released version and the version the package reports can never
drift apart, and the whole release is one reviewable commit. The price is that the workflow
runs on every push to `main`; the tag check in step 1 keeps that to a few seconds.

## Doing it by hand

```sh
make check
make build                 # dist/*.whl, dist/*.tar.gz and .local/build
docker build .local/build  # the same image the workflow builds
```

## Versioning

[Semantic versioning](https://semver.org). Note the ephios version this release is built and
tested against: the dependency is pinned exactly, so a new ephios needs a new release here.
Record what changed in [CHANGELOG.md](../CHANGELOG.md) in the same commit as the version bump.

## Publishing to PyPI

Not wired up. If the package should be installable with `pip install`, add a job that uses
PyPI's trusted publishing with the repository's OIDC identity instead of a stored token.
