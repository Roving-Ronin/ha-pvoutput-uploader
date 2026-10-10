<img src="docs/images/icon.svg" alt="" width="96" align="right">

# PVOutput Uploader for Home Assistant

Sends Home Assistant sensors to [PVOutput](https://pvoutput.org) and keeps its own
history log, so data can be sent again later. Modelled on the PVOutput service of the
IoTaWatt firmware: a local log, uploads that resume where they stopped, rate limit
handling, and reloading history from a chosen date.

## How it works

- **History log.** Every 5 minutes the values of all mapped sensors are taken from the
  Home Assistant recorder and stored in `pvoutput_uploader_<system id>.db` in the
  configuration folder (part of HA backups). The log is always 5 minute resolution,
  whatever the upload interval, and is kept for 400 days by default.
- **Uploads.** Statuses are built from the log at the upload interval (read from the
  system's status interval in PVOutput, or set by hand) and sent oldest first. After an
  outage the backlog is sent automatically, as long as PVOutput still accepts it.
- **Rate limit.** The `X-Rate-Limit-*` headers of every answer are tracked. When the
  hourly limit is reached uploading pauses until the reset time and then continues;
  the pause survives restarts.
- **Donor features** are detected from the account (or set by hand): extended fields
  v7-v12, battery fields b1-b6, statuses up to 90 days back (14 otherwise) and 100
  statuses per request (30 otherwise).
- **Battery fields** are not accepted by PVOutput's batch service, so while any battery
  field is active every status is sent with its own `addstatus` request.

## Configure menu

Everything is under **Settings → Devices & services → PVOutput Uploader → Configure**.
Each action saves as soon as it is submitted.

- **Add field**: first choose where the value comes from:
  - **A sensor**: pick the sensor and the PVOutput field it fills (only fields not used
    yet, and donor fields only for donor accounts). Then name it as in PVOutput, choose
    whether it is **active**, and, where a field allows it, how the 5 minute values of an
    interval are combined: average, last value, or energy since midnight.
  - **A fixed value**: the same number in every status, e.g. the battery size for `b3`.
    It is not logged and never sends a status on its own.
- **Edit field** / **Remove fields**.
- **Reupload history**: sends a date range of the log again (see below).
- **Import history from Home Assistant**: fills the log for a time before the integration
  was installed (see below).
- **Settings**: account type, upload interval, history retention, daily output fallback.

**Passive** fields are recorded in the log but not sent. Make one active later and
reupload to send its history.

Only active fields are sent; everything else is left empty. This lets the integration
add values (temperature, extended, battery) to statuses that another uploader such as
IoTaWatt creates. PVOutput needs v1-v4 in a status, so a status holding only extra
fields is retried for up to two hours, waiting for the other uploader to create it.

| Field | Meaning | Sent as |
|---|---|---|
| v1 / v3 | Energy generation / consumption | Wh since local midnight, from an energy sensor (lifetime or daily counters both work) |
| v2 / v4 | Power generation / consumption | W, average or last |
| v5 | Temperature | °C |
| v6 | Voltage | V |
| v7-v12 | Extended (donor) | average, last, or energy since midnight |
| b1-b6 | Battery power, SoC, size, lifetime charge/discharge, state (donor) | |

Sensor units are converted (kWh to Wh, kW to W, °F to °C, ...). Sensors must be
recorded by the HA recorder.

## Reupload and import

Statuses that could not be sent (uploading switched off, PVOutput or network errors,
rate limit) stay in the log and are sent automatically, oldest first, when uploading can
continue. Statuses PVOutput rejected are not retried on their own.

**Reupload history** (Configure menu, or the `pvoutput_uploader.reupload` action for
scripts and automations) sends the active fields again, in the background, as fast as
PVOutput's hourly request limit allows. Like IoTaWatt's reload, it starts either
**since the first unsent status** (the oldest one waiting, rejected or too old;
`since_failure: true` in the action) or **from a date and time** you choose, up to an end
day (default today), sent as:

- *Statuses where allowed, daily totals for older days* (default): days inside PVOutput's
  status window (14 days, 90 for donors) as statuses, older days as daily outputs
- *Statuses only*: only the days still inside the status window
- *Daily totals only*: daily outputs (generation, consumption, peak power, min/max
  temperature); these replace the day's totals in PVOutput

The **Samples waiting** and **Daily outputs waiting** sensors show the progress.

**Import history from Home Assistant** (Configure menu, or the
`pvoutput_uploader.import_from_recorder` action) fills the log for a past period, e.g.
right after installing. Recorded states are used where they still exist (10 days by
default), hourly long term statistics before that (split evenly into 5 minute rows).
With *Send to PVOutput* off the rows are only stored, ready for a later reupload; with it
on they are sent like any other backlog.

## Entities

- **Uploading** switch: stops sending while recording carries on.
- Sensors: last upload, samples waiting, daily outputs waiting, rejected samples, history
  size and start, requests left this hour, rate limit pause, account type, last error.

## Settings

- Account type: detect, donor or standard
- Upload interval: from PVOutput, 5, 10 or 15 minutes
- History retention in days
- Daily output fallback: when statuses become too old to send, send that day's
  totals as a daily output instead (off by default, because it overwrites the day's
  output in PVOutput)

## Development

Tests run in a test Home Assistant:

```bash
pip install pytest-homeassistant-custom-component
python -m pytest tests
```

The recorder's libraries (SQLAlchemy and others, listed in Home Assistant's
`components/recorder/manifest.json`) must be installed too; CI does this.

Releasing: bump `version` in `manifest.json`, add a `## <version>` section to
`CHANGELOG.md` and push to `main`. When the tests, hassfest and the HACS checks pass,
the release workflow tags `v<version>` and publishes a GitHub release (a pre-release
for versions such as `0.0.1b1`).

The icon's source is [`docs/images/icon.svg`](docs/images/icon.svg); the PNGs in
`custom_components/pvoutput_uploader/brand/` (256 and 512 px) are rendered from it.
Home Assistant 2026.3+ shows them in place of the brands repository.
