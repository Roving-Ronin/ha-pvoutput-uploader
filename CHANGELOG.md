# Changelog

## 0.1.6b6

- **Make fields active or passive in one go.** Each field has a **Send <field>** switch on the PV site's device page (under Configuration), and **Configure → Active fields** lists every field on one page with a tick for active. Both save the field's Active setting; the site reloads to apply it.
- The cleanup of removed fields' sensors now only looks at sensors.

## 0.1.6b5

- **Reupload history → Also send passive fields.** Passive fields are recorded but not sent; a reupload can now send the ones you tick (all by default) for the statuses of the chosen period only, while live statuses keep them off. Donor and battery rules still apply, and the next reupload replaces the choice. Shown only when the site has passive fields; the `reupload` action takes `passive_fields`.

## 0.1.6b4

- **Reupload history → To** takes a time as well as a day, like From: statuses up to that time are sent (default now). The `reupload` action's `end` takes a date and time, or a day as before (up to its end).
- Battery values in batches are confirmed: PVOutput stores them (a few days of history with battery values went out in a moment). README updated.

## 0.1.6b3

- **Battery fields go in batches.** Statuses with b1-b6 are sent in batches like the others (up to 100 per request for donors), as six columns after `v12`, instead of one request per status: a 3-week backlog takes about 61 requests instead of about 6,000. PVOutput documents battery fields only for single statuses; a test batch with them was accepted. If battery values are missing in PVOutput after an upload, please report it. The **Leave out battery fields** tick box is removed from Reupload history as it no longer saves time (`without_battery` on the action still works).
- **Clear queued history** button on the PV site: takes the waiting history off the queue (statuses older than the last two upload intervals, and queued daily totals) and ends a "leave out battery fields" run, while current statuses keep going. Nothing is deleted: cleared statuses stay in the log as unsent, counted in the new **Cleared samples** sensor, and **Reupload history** (also since the first unsent status) sends them again.
- Removed a stray `config_flow.py.bak` that had been shipped since 0.1.5.

## 0.1.6b2

- **The history reserve is worked out, not fixed.** History stops when the requests left would only just cover every current status due before PVOutput's hourly limit resets, plus one, on top of the reserve of 5 for other uploaders. A 5 minute site with 40 minutes to go keeps 14; near the end of the hour, 7. Before the reset time is known, a full hour is assumed (18 at 5 minutes). Replaces the fixed 30 from 0.1.6b1, so a backlog gets almost the whole hour.

## 0.1.6b1

- **Current statuses go first.** The statuses of the last two upload intervals are sent before any history, so live data no longer waits behind a long backlog (it was oldest first).
- **History stops earlier than live data.** Backlog, reuploads and daily totals stop while 30 requests are left in the hour; current statuses can go on down to the reserve of 5. 25 requests an hour cover a 5 minute site sending battery fields, even two.
- The last 5 requests of each hour (was 2) are kept in reserve. PVOutput's hourly limit belongs to the API key, so other uploaders using the same key, such as IoTaWatt, keep room while a backlog is sent.

## 0.1.5

- **Reupload history → Leave out battery fields (much faster)**, shown only when battery fields are sent: everything waiting up to the moment you submit is sent in batches of 100 without battery values (b1-b6) instead of one request per status, so weeks of history go out in minutes rather than a day. Statuses holding only battery values are skipped, new statuses keep their battery values, and reuploading the period again without the tick adds the battery values. The `reupload` action takes `without_battery`.

## 0.1.4

- Adding a field checks again on submit, against the configuration as saved at that moment, that the PVOutput field is still free. A field added meanwhile in another Configure window (or from a stale form) is no longer added twice, and saving a new field no longer drops one another window saved. The field lists already showed only free fields.
- When every field the account can send is configured, **Add field** is not shown, and the menu says why (on a standard account: all of v1-v6, as extended and battery fields need a donor account). If the last free field is taken in another window after the menu opened, adding stops with that message instead of showing an empty list.
- README: each field can be used once; how the free-field lists and the checks work.

## 0.1.3

- **b1 (battery power) is mandatory for battery data**, as PVOutput ignores b2-b6 without it: **Add field** offers b2-b6 once b1 is configured, b1 cannot be removed while other battery fields remain, and without an active b1 the other battery fields are not sent. Their field sensors explain why.
- **b6 (battery state) as a fixed value is picked from a list** of PVOutput's states (0 Invalid, 1 Standby, 2 Thermal management, 3 Enabled, 4 Fault, 5-9 user defined), when adding and when editing. Its field sensor shows the state's name. As a sensor, b6 works like the other fields.
- README: how to work out a b6 state for a battery that reports none (e.g. a Victron SmartShunt), from battery power and its alarms.

## 0.1.2

- Prepare for inclusion in the default HACS catalogue: enable all HACS validation checks without exceptions.
- No change to uploader behaviour compared with 0.1.1.

## 0.1.1

- PVOutput requests are sent at least 0.3 seconds apart, so a backlog (for example after uploading was switched off) goes out at a steady pace instead of in a burst. The hourly request limit still sets how fast a backlog clears.

## 0.1.0 — First stable release

PVOutput Uploader sends your Home Assistant solar, consumption and battery readings to PVOutput, with field mapping and history controls in the Home Assistant UI.

### Set up your PV site

Use **Add PV Site**, enter your PVOutput API key and system ID, then open **Configure** to add sensor fields or fixed values. The integration reads your account's donor status and upload interval. Donor accounts can also use extended and battery fields.

Each field on the PVOutput device page shows the sensor's actual friendly name and the upload unit, such as **Outside °C** or **Battery Power W**. Fixed values show their units too—for example, **5544 Wh** for battery capacity. For sensors without a reported unit, you can specify a source unit in Add field or Edit field; supported units are validated and converted for PVOutput.

### Keep and recover your data

- Readings are recorded into a local history log every five minutes. Uploading can be switched off while recording continues; passive fields are recorded for later use.
- Network failures, PVOutput outages and rate limits leave data queued for automatic upload when sending can resume. The queue and retry timing survive restarts.
- Rejected samples and daily outputs remain visible. If PVOutput identifies a rejected value, the uploader makes one attempt without that value so the other fields can still arrive; it records the result as partly sent and keeps the complete sample available for reupload.
- **Reupload history** can start from the first unsent status or a selected date and time. **Import history from Home Assistant** uses recorded states where available, then hourly long-term statistics for older periods.
- Status history is sent within PVOutput's account limits. Older days can be sent as daily totals. Recovery detail depends on the history still available in the integration and Home Assistant; unavailable sensor readings leave gaps rather than fabricated values.

The device page includes upload, backlog, rejection and rate-limit diagnostics. The default local history retention is 91 days and can be changed in Configure.

### Upgrading from a beta

Update through HACS and restart Home Assistant. Existing configuration and history are retained. This release includes all changes from b6 plus the **Add PV Site** button and setup title.

Unit changes apply to new readings or history you explicitly rebuild; existing logged samples are not automatically rewritten.

## 0.0.1b6 — Clearer field display and support for sensors without units

The PVOutput device page now shows readable source names and the units PVOutput receives, making it easier to check your field mappings at a glance.

### Friendly names and visible units

- Sensor-backed fields use the selected Home Assistant entity's actual friendly name instead of its entity ID. The original ID remains available in the `source_entity` attribute.
- Field states include the upload unit: for example, **Outside °C**, **Battery Power W**, and a fixed battery capacity of **5544 Wh**. Battery state of charge shows **%**.
- The `pvoutput_unit` and `source_unit` attributes distinguish the upload unit from the sensor's unit. Supported source units are converted to the units PVOutput expects.

### Sensors without a unit

- **Add field** and **Edit field** now let you specify a **Source / custom unit** when a sensor does not report one, such as **kW**, **kWh**, or **°F**. The sensor's own unit takes precedence when present.
- Units are checked before saving standard fields. A unitless temperature sensor configured as °F is converted to °C; a unitless power sensor configured as kW is converted to W.
- Explicit source units are also respected when importing history whose statistics metadata has no unit, without converting already-normalized statistics twice.
- Extended fields can use custom units. Fixed values for standard fields are entered directly in PVOutput's expected unit—for example, battery capacity in **Wh**.

### Upgrading

Update through HACS and restart Home Assistant. Existing mappings remain compatible; use **Configure → Edit field** to supply a missing sensor unit if needed.

Unit changes apply to newly recorded or explicitly rebuilt history. Samples already stored in the integration's history log are not automatically rewritten.

## 0.0.1b5

- When PVOutput rejects a status for a value it names (power or energy too high, generation at night), the status is sent once more without that value, so the other fields still arrive. It counts as **Partly sent** (new diagnostic sensor) and stays unsent, so **Reupload history → since the first unsent status** sends it whole once fixed. Other rejections are not retried.
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
