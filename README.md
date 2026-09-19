# ephios shift coordination

Availability surveys, automatic service planning and assemblies for
[ephios](https://ephios.de).

Members say when they can help, the plugin proposes a plan that keeps every rule, and
publishing writes ordinary ephios participations. Assemblies are called with one short form
and answered straight from the invitation mail.

**[Screenshots and feature tour](docs/screenshots.md)**

## What it does

- **Planning periods.** One service template becomes a series of ephios events: pick a date
  range, tick the days, create them. Holidays can be skipped, and the plugin never touches
  events it did not create.
- **Availability surveys.** One compact table per member: a rating per shift, a personal
  maximum and a private note. Invitations and reminders go out through ephios' own
  notification settings; the deadline locks answers server-side.
- **Automatic proposals.** A solver fills as many shifts as possible, then minimises people
  sitting in, then assignments marked "if needed", then maximises strong preferences. Every
  proposed shift reaches its minimum staffing or stays empty.
- **Shared drafts.** Coordinators edit one draft together. Rule violations are marked in
  place and confirmed once, with an optional note that is kept for the record.
- **Publication.** Confirmed native participations, one summary message per person, and a
  record of what was published, by whom and with which exceptions.
- **Replacement.** After publication everybody staffed can sign off, step in, or offer to sit
  in, and sees who else answered they are available that day.
- **Sitting in.** Joining a service without taking a shift: no qualification, no working
  hours, but counted towards the staffing.
- **Assemblies.** Called from one form, invited with an agenda, answered from the mail
  without a login, with minutes filed as PDF and found again through one search.
- **Reminders.** Before a service, before an assembly, and before the next planning period is
  due, each one going out once per person and appointment.
- **Public duty information.** Three read-only endpoints for a display at the station, off by
  default.

## Requirements

ephios 0.27, Python 3.14, and a PostgreSQL database. The solver needs SciPy, which the
package installs.

## Installation

The container image bundles ephios and the plugin, and every
[release](https://github.com/islpriem/ephios-shift-coordination-plugin/releases) publishes one:

```
ghcr.io/islpriem/ephios-shift-coordination-plugin:<version>
```

Alternatively install that release's wheel into the Python environment your ephios runs in.
Either way, enable **Shift coordination** in ephios under *Settings → Instance*, then give the
coordinating groups their rights in *Settings → Planning settings*.

[Deployment](docs/deployment.md) · [Development](docs/development.md) ·
[Releases](docs/releases.md)

## License

MIT, see [LICENSE](LICENSE).
