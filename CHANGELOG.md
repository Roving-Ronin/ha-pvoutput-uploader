# Changelog

## 0.0.1b2

- Options menu: separate **Add sensor field** and **Add constant field**; editing a field shows only the inputs of its kind.
- Tests for the builder, store, API client, uploader, flows, setup and actions; releases now wait for them, hassfest and the HACS checks.

## 0.0.1b1

First beta.

- Field mapping from the UI: add fields one at a time (sensor or constant to a PVOutput field), name them as in PVOutput, mark them active or passive. Donor-only fields (v7-v12, b1-b6) are offered only to donor accounts.
- 5 minute history log in SQLite, kept 400 days by default, built from the recorder.
- Uploads at the PVOutput status interval (or 5/10/15 minutes), oldest first, with batching, and single `addstatus` requests while battery fields are active.
- Rate limit handling: pauses at the hourly limit and resumes after the reset, also after restarts.
- `reupload` action (statuses inside the window, daily outputs before it) and `import_from_recorder` action (recorded states, then hourly statistics).
- Uploading switch and status sensors.
