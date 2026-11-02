# Changelog

This project follows [semantic versioning](https://semver.org). Every entry names the ephios
versions it was built and tested against.

## Unreleased

For ephios 0.27.

- Allow concurrent regular duty and sitting in on sibling shifts of the same event in
  proposals, validation, publication and later self-service staffing.
- Exclude red survey answers from both automatic roles, even with an observer offer or
  an own regular duty at that event.
- Include existing observer participations in future planning, stale-data checks and
  publication locks, respecting event types and individual participation times.
- Correct replacement suggestions and displayed personal loads for concurrent roles.
- Allow changing qualified candidates between regular duty and sitting in in the manual
  planner; keep unsaved role changes protected and show the proposal's optimality status.
- Add exhaustive role comparisons, publication, concurrency, browser and load regressions;
  document the algorithm and its limits in [Scheduling](docs/scheduling.md).

## 1.0.0 — 19 September 2026

First release, for ephios 0.27.

- Planning periods from a service template, with holiday handling and a calendar selection.
- Availability surveys with per-shift ratings, a personal maximum, private notes, invitations,
  reminders and a server-side deadline.
- Automatic proposals from a lexicographic solver, shared drafts, recorded exceptions and
  publication into native ephios participations.
- Replacement overview with self-service sign-off and stepping in after publication.
- Sitting in on a service without taking a shift.
- Assemblies with agenda, invitation, answering from the mail without a login, and minutes
  filed as PDF.
- Reminders before services, assemblies and the next planning period.
- Three read-only endpoints with public duty information, off by default.
