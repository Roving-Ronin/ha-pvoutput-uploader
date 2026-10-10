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

## Fields

Fields are added one at a time under **Settings → Devices & services → PVOutput
Uploader → Configure → Add field**:

1. Pick a sensor (or enter a constant, e.g. battery size for `b3`) and the PVOutput
   field it feeds. Only unused fields that the account type allows are offered.
2. Give it the name used in PVOutput, choose whether it is **active**, and for fields
   that allow it, how the 5 minute values are combined: average, last value, or energy
   since midnight.

**Passive** fields are recorded in the log but not sent. Make one active later and
run `pvoutput_uploader.reupload` to send its history.

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

## Actions

### `pvoutput_uploader.reupload`

Sends history from the log again from `start` to `end` (default today).

- `auto`: days inside PVOutput's status window as statuses, older days as daily outputs
- `statuses`: only the days still inside the status window
- `outputs`: daily outputs only (generation, consumption, peak power, min/max temperature)

### `pvoutput_uploader.import_from_recorder`

Fills the log for a past period, e.g. right after installing. Recorded states are used
where they still exist (10 days by default), hourly long term statistics before that
(split evenly into 5 minute rows). With `upload` off the rows are only stored; with it
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
