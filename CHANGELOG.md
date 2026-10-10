# Changelog

## 0.0.1b5

- Field sensor attribute `sent` renamed to `configured_to_send` (and `not_sent_because` to `not_configured_to_send_because`): it means the field is active and allowed by the account type, not that uploading is on or that PVOutput accepted it.

## 0.0.1b4

- One sensor per configured field on the PVOutput system's device: named after the PVOutput field and its label, showing the Home Assistant sensor it reads (or the fixed value), whether it is sent, how values are combined and the last recorded value. The status sensors moved to Diagnostic, so the device's Sensors card lists exactly what is uploaded.
- A daily output PVOutput rejects stays in the log as rejected, shown in the new **Rejected daily outputs** sensor, instead of disappearing from the queue; reuploading the day sends it again. A rejected output no longer counts as an upload.
- The start of the two hour retry window for unconfirmed statuses is kept in the history log, so restarts and option changes do not start it again.
- README: daily totals are only sent for days too old for statuses; PVOutput works out the totals of other days from the statuses.

## 0.0.1b3

- The history log is kept for 91 days by default (was 400): the 90 days donors can send statuses for, plus today. Existing installs keep their setting (Configure → Settings → Keep history for).
- A "last value" field is not sent when the sensor was unavailable at the end of the interval, and energy since midnight is only sent for intervals with a counter reading, so old values are not repeated as current.
- The two hour retry for unconfirmed statuses counts from the first unconfirmed attempt, so reuploaded and imported history gets it too.
- README: where the data lives (the history log, the recorder, long term statistics) and which disruptions need nothing from you. The default limits (history log 91 days, recorder 10 days, statistics forever) and how to change them are described in the README and under the setting.

## 0.0.1b2

- Configure menu reworked: **Add field** asks first whether the value comes from a sensor or is a fixed value; editing a field shows only the inputs of its kind; every action saves when submitted (no separate save step); explanations on every screen.
- **Reupload history** and **Import history from Home Assistant** are in the Configure menu, besides the actions.
- Reupload starts **since the first unsent status** (waiting, rejected or too old) or **from a date and time**, like IoTaWatt's reload; the action takes `since_failure` or a date or datetime `start`.
- Integration icon (shown by Home Assistant 2026.3+), entity and action icons.
- Only statuses PVOutput confirms (`1` in the batch answer) count as sent; unconfirmed ones, and statuses refused with "Missing energy and power values" while another uploader has not created them yet, are retried for two hours.
- Batches of daily outputs go to `addoutput.jsp` (there is no `addbatchoutput.jsp`).
- An unavailable sensor leaves a gap: no last value and no energy for a slot without a counter reading, instead of repeating an old reading or logging zero.
- Tests for the builder, store, API client, uploader, flows, setup and actions; releases now wait for them, hassfest and the HACS checks.

## 0.0.1b1

First beta.

- Field mapping from the UI: add fields one at a time (sensor or constant to a PVOutput field), name them as in PVOutput, mark them active or passive. Donor-only fields (v7-v12, b1-b6) are offered only to donor accounts.
- 5 minute history log in SQLite, kept 400 days by default, built from the recorder.
- Uploads at the PVOutput status interval (or 5/10/15 minutes), oldest first, with batching, and single `addstatus` requests while battery fields are active.
- Rate limit handling: pauses at the hourly limit and resumes after the reset, also after restarts.
- `reupload` action (statuses inside the window, daily outputs before it) and `import_from_recorder` action (recorded states, then hourly statistics).
- Uploading switch and status sensors.
