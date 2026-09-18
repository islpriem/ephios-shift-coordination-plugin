# Deployment

## What you install

The plugin is an ordinary ephios plugin: a Python package that registers itself through the
`ephios.plugins` entry point. It has to live in the same Python environment as ephios 0.27.0.

Two ways to get there.

### Container image

`ghcr.io/islpriem/ephios-shift-coordination-plugin:<version>` is the official ephios 0.27.0
image with the plugin installed into its environment. It keeps ephios' own entrypoint, so
migrations, static files, translations and the periodic command work unchanged. Point your
existing ephios compose service at this image instead of `ghcr.io/ephios-dev/ephios`; every
other part of your setup — database, proxy, mail, volumes — stays as it is.

The image is `linux/amd64`, because the ephios base image is.

### Wheel

Download `ephios_shift_coordination_plugin-<version>-py3-none-any.whl` from the
[releases](https://github.com/islpriem/ephios-shift-coordination-plugin/releases) and install
it into the environment ephios runs in, then restart ephios. In an uv-managed ephios checkout
that is `uv add /path/to/the.whl`.

## Turning it on

1. Sign in as an ephios administrator and enable **Shift coordination** under
   *Settings → Instance*.
2. Open *Settings → Planning settings* and pick the groups that may coordinate. Those groups
   additionally need ephios' own rights to create events and to publish them for the groups a
   service template is visible to — the plugin never grants those silently.
3. Create a service template under *Settings → Service templates* with the real shift times,
   qualifications and minimum and maximum staffing.
4. Mark the event types you use under *Settings → Event types*: whether events of that type
   are services, assemblies, or neither.

## Periodic tasks

Invitations, reminders and the deadline lock ride on ephios' `run_periodic` command, which the
official image already runs. It has to run at least every 15 minutes. Nothing in the plugin
needs a worker, a queue or a second process.

Answers are locked at the deadline by the server itself, so a late periodic run delays
reminders but never accepts an answer it should not.

## Data the plugin stores

Its own tables hold planning periods, survey answers, drafts, recorded exceptions, the
publication record, assemblies and their minutes. Uploaded minutes are PDFs under ephios'
`MEDIA_ROOT`, which is never served directly. Everything else — events, shifts, participations,
notifications — is native ephios data.

A backup that covers the ephios database and its data directory therefore covers the plugin.

## Upgrading

Upgrade by moving to a new image tag or installing a newer wheel, then let ephios' entrypoint
run the migrations. Downgrading an image does not undo database migrations; the safe way back
is the matching earlier version together with a backup from before the upgrade.

## Public duty information

Three read-only endpoints answer without a session, for a display at the station:

```
GET /shift-coordination/api/now/     {"duty": true, "until": "2026-09-18T17:00:00+02:00"}
GET /shift-coordination/api/next/    {"start": "2026-09-19T09:00:00+02:00"}
GET /shift-coordination/api/week/    {"from": "2026-09-14", "days": [true, false, …]}
```

They answer 404 until an administrator enables them in the planning settings and picks the
event types to count. Only shifts that reach their minimum staffing count as duty, and the
answers carry times and truth values only: no names, no head counts, no event titles.
