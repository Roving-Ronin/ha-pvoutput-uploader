"""PVOutput history kept as hourly long-term statistics (the archive).

Every day PVOutput holds statuses for is read with getstatus.jsp?h=1, rolled
up to hours and stored in the log database, from which external statistics
``pvoutput_uploader:<system id>_<field>`` are (re)built: energy as an hourly
sum, everything else as hourly mean/min/max. All columns PVOutput returns are
kept, whatever the fields are set to. A field in Download mode also gets the
archive copied into its download sensor's statistics, for the hours before
the sensor's own statistics begin.
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    ElectricPotentialConverter,
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
)

from .api import HistoryStatus, PVOutputRateLimited
from .const import (
    AGG_ENERGY_TODAY,
    CONF_SYSTEM_ID,
    DOMAIN,
    FIELDS_BY_KEY,
    KIND_ENERGY,
    KIND_POWER,
    KIND_TEMPERATURE,
    KIND_VOLTAGE,
    MAX_REQUESTS_PER_CYCLE,
    upload_unit,
)

if TYPE_CHECKING:
    from .uploader import Uploader

_LOGGER = logging.getLogger(__name__)

NORMALISED = "normalised"
ARCHIVE_FIELDS = (*(f"v{n}" for n in range(1, 13)), NORMALISED)
# Energy columns are counted since local midnight; v7-v12 too where the field
# is set up as energy since midnight.
ENERGY_FIELDS = ("v1", "v3")
NORMALISED_UNIT = "kW/kW"

# The daily download fetches yesterday from this local hour on, so late
# statuses (e.g. another uploader's backlog) are in.
DAILY_HOUR = 2
# After a stop, missed days are caught up this far back at most.
CATCH_UP_DAYS = 30
# Days before yesterday fetched again each day, for statuses that came late.
REFRESH_DAYS = 2
# A long history download rebuilds the statistics every this many days with
# detail (and when it ends), not after every batch.
IMPORT_EVERY_DAYS = 30

META_BACKFILL = "archive_backfill"
META_BACKFILL_RESULT = "archive_backfill_result"
META_DAILY_LAST = "archive_daily_last"
META_DAILY_REFRESHED = "archive_daily_refreshed"
# Changed archive hours not imported into statistics yet (earliest hour).
META_IMPORT_FROM = "archive_import_from"

UNIT_CLASSES = {
    KIND_ENERGY: EnergyConverter.UNIT_CLASS,
    KIND_POWER: PowerConverter.UNIT_CLASS,
    KIND_TEMPERATURE: TemperatureConverter.UNIT_CLASS,
    KIND_VOLTAGE: ElectricPotentialConverter.UNIT_CLASS,
}


def statistic_id(system_id: str, field: str) -> str:
    """Return the external statistic of a field's PVOutput history."""
    return f"{DOMAIN}:{system_id}_{field}"


def day_window(day: date) -> tuple[int, int]:
    """Return the UTC epoch range [start, end) of a local day."""
    start = dt_util.start_of_local_day(day)
    end = dt_util.start_of_local_day(day + timedelta(days=1))
    return int(start.timestamp()), int(end.timestamp())


def _status_ts(day: date, hhmm: str) -> int | None:
    """Return the epoch of a status time; None for 00:00 or a bad time.

    The 00:00 status closes the previous day (its energy is already 0 for
    the new day), so the previous day's statuses cover that hour.
    """
    try:
        hour, minute = (int(part) for part in hhmm.split(":"))
    except ValueError:
        return None
    if hour == 0 and minute == 0:
        return None
    local = datetime.combine(day, dt_time(hour, minute), dt_util.get_default_time_zone())
    return int(local.timestamp())


def rollup(
    day: date, statuses: list[HistoryStatus], energy_fields: set[str]
) -> list[tuple[int, str, Any, Any, Any, Any]]:
    """Roll a day's statuses up to hours: (hour, field, mean, min, max, energy).

    A status covers the interval ending at its time, so 10:00 counts towards
    09:00-10:00. Energy (counted since midnight) becomes the change over each
    hour; other values the mean, min and max of the hour's statuses.

    Where hours have no statuses (a gap, or from midnight to the first
    status), the energy counted over them is only known in total: it is
    shared evenly by the hours it covers (the gap and the next hour with a
    status), as Import history does. The day's total stays exact.
    """
    day_start = day_window(day)[0]
    buckets: dict[str, dict[int, list[float]]] = {}
    for status in statuses:
        ts = _status_ts(day, status.time)
        if ts is None:
            continue
        hour = (ts - 1) // 3600 * 3600
        for field, value in status.values.items():
            buckets.setdefault(field, {}).setdefault(hour, []).append(value)
    rows = []
    for field, hours in buckets.items():
        if field in energy_fields:
            previous, previous_hour = 0.0, day_start - 3600
            for hour in sorted(hours):
                last = hours[hour][-1]
                covered = range(previous_hour + 3600, hour + 3600, 3600)
                share = max(last - previous, 0.0) / len(covered)
                rows.extend((h, field, None, None, None, share) for h in covered)
                previous, previous_hour = last, hour
        else:
            for hour, values in sorted(hours.items()):
                rows.append(
                    (hour, field, sum(values) / len(values), min(values), max(values), None)
                )
    return rows


class Archive:
    """Downloads PVOutput history into the archive and its statistics."""

    def __init__(self, uploader: Uploader) -> None:
        """Initialize the archive."""
        self.up = uploader
        self.hass = uploader.hass
        # The Daily history download switch.
        self.enabled = True
        # {"next": day, "end": day, "detail": n, "empty": n} while backfilling.
        self.backfill: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.summary: dict[str, Any] = {}
        # Earliest archive hour changed since the statistics were last built.
        self._import_from: int | None = None
        self._fetched_since_import = 0
        # Copy into download sensors once the entities exist, and after imports.
        self._copy_due = True

    @property
    def system_id(self) -> str:
        """Return the PVOutput system ID."""
        return str(self.up.entry.data[CONF_SYSTEM_ID])

    @property
    def days_left(self) -> int:
        """Return the days still to fetch for a history download."""
        if self.backfill is None:
            return 0
        start = date.fromisoformat(self.backfill["next"])
        end = date.fromisoformat(self.backfill["end"])
        return max((end - start).days + 1, 0)

    async def async_load(self) -> None:
        """Restore a history download in progress."""
        store = self.up.store
        if raw := await self.up._run(store.get_meta, META_BACKFILL):
            self.backfill = json.loads(raw)
        if raw := await self.up._run(store.get_meta, META_BACKFILL_RESULT):
            self.result = json.loads(raw)
        if raw := await self.up._run(store.get_meta, META_IMPORT_FROM):
            self._import_from = int(raw)
        self.summary = await self.up._run(store.archive_summary)

    def _energy_fields(self) -> set[str]:
        return set(ENERGY_FIELDS) | {
            m.field
            for m in self.up.mappings
            if m.aggregate == AGG_ENERGY_TODAY and m.field not in ENERGY_FIELDS
        }

    def _unit(self, field: str) -> tuple[str | None, str | None]:
        """Return the unit and unit class of a field's statistics."""
        if field == NORMALISED:
            return NORMALISED_UNIT, None
        mapping = next((m for m in self.up.mappings if m.field == field), None)
        aggregate = mapping.aggregate if mapping else None
        unit = upload_unit(field, aggregate, mapping.unit if mapping else None)
        kind = KIND_ENERGY if aggregate == AGG_ENERGY_TODAY else FIELDS_BY_KEY[field].kind
        return unit, UNIT_CLASSES.get(kind) if unit else None

    def _name(self, field: str) -> str:
        label = "Normalised output" if field == NORMALISED else FIELDS_BY_KEY[field].name
        return f"{self.up.entry.title} {field} {label} (PVOutput history)"

    # Fetching

    async def _fetch_day(self, day: date, *, store_it: bool = True) -> bool | None:
        """Fetch one day. Return whether PVOutput had statuses, None if paused."""
        up = self.up
        if not up._rate_ok():
            return None
        await up._pace()
        try:
            statuses = await up.client.get_status_history(
                day.strftime("%Y%m%d"), extended=up.donor
            )
        except PVOutputRateLimited as err:
            await up._pause(err.reset, str(err))
            return None
        if not store_it:
            return bool(statuses)
        if statuses:
            # PVOutput's detail replaces what the archive held for the day; a
            # day without detail keeps it.
            start, end = day_window(day)
            rows = rollup(day, statuses, self._energy_fields())
            await up._run(up.store.archive_replace, start, end, rows)
            self._import_from = start if self._import_from is None else min(
                self._import_from, start
            )
            self._fetched_since_import += 1
        await up._run(up.store.archive_mark_day, day.isoformat(), bool(statuses))
        return bool(statuses)

    async def async_first_detail(self, start: date, end: date) -> date | None:
        """Return the first day from ``start`` PVOutput has statuses for.

        Probes ``start``, then the end, then halves the range, assuming
        PVOutput's detail runs without long gaps up to now. About log2(days)
        requests. None if no day in range has detail. When the hourly limit
        stops the probing, ``start`` is returned: the whole range is fetched.
        """
        found = await self._fetch_day(start, store_it=False)
        if found or found is None:
            return start
        found = await self._fetch_day(end, store_it=False)
        if found is None:
            return start
        if not found:
            return None
        low, high = start, end  # low: no detail, high: detail
        while (high - low).days > 1:
            mid = low + timedelta(days=(high - low).days // 2)
            found = await self._fetch_day(mid, store_it=False)
            if found is None:
                break
            if found:
                high = mid
            else:
                low = mid
        return high

    async def async_queue(self, start: date, end: date) -> None:
        """Queue a history download of the days ``start`` to ``end``."""
        self.backfill = {
            "next": start.isoformat(),
            "end": end.isoformat(),
            "detail": 0,
            "empty": 0,
        }
        await self._save_backfill()

    async def _save_backfill(self) -> None:
        raw = json.dumps(self.backfill) if self.backfill else None
        await self.up._run(self.up.store.set_meta, META_BACKFILL, raw)

    async def async_cycle(self) -> None:
        """Run the history download and the daily download, then import."""
        before = self._import_from
        try:
            await self._async_backfill()
            if self.enabled:
                await self._async_daily()
        finally:
            # A long history download rebuilds the statistics now and then and
            # at its end, not after every batch: each rebuild runs from the
            # earliest changed hour to the newest, to keep energy totals whole.
            if self._import_from is not None and (
                self.backfill is None or self._fetched_since_import >= IMPORT_EVERY_DAYS
            ):
                await self._async_import(self._import_from)
                self._import_from = None
                self._fetched_since_import = 0
                self._copy_due = True
            if self._import_from != before:
                await self.up._run(
                    self.up.store.set_meta,
                    META_IMPORT_FROM,
                    None if self._import_from is None else str(self._import_from),
                )
            if self._copy_due:
                self._copy_due = False
                await self._async_copy_to_sensors()
            self.summary = await self.up._run(self.up.store.archive_summary)

    async def _async_backfill(self) -> None:
        if self.backfill is None:
            return
        job = self.backfill
        for _ in range(MAX_REQUESTS_PER_CYCLE):
            day = date.fromisoformat(job["next"])
            if day > date.fromisoformat(job["end"]) or self.backfill is not job:
                break
            found = await self._fetch_day(day)
            if found is None:
                break
            job["detail" if found else "empty"] += 1
            job["next"] = (day + timedelta(days=1)).isoformat()
        if self.backfill is not job:
            # A new download was queued meanwhile (Configure); it replaces this one.
            return
        if date.fromisoformat(job["next"]) > date.fromisoformat(job["end"]):
            self.result = {
                "detail": job["detail"],
                "empty": job["empty"],
                "finished": dt_util.utcnow().isoformat(),
            }
            await self.up._run(
                self.up.store.set_meta, META_BACKFILL_RESULT, json.dumps(self.result)
            )
            self.backfill = None
        await self._save_backfill()

    async def _async_daily(self) -> None:
        """Fetch the new days, and once a day the days before yesterday again.

        Statuses can reach PVOutput late (another uploader catching up), so
        each new day also fetches the REFRESH_DAYS before it once more.
        """
        store = self.up.store
        now = dt_util.now()
        latest = now.date() - timedelta(days=1 if now.hour >= DAILY_HOUR else 2)
        stored = await self.up._run(store.get_meta, META_DAILY_LAST)
        last = date.fromisoformat(stored) if stored else None
        if last is None:
            days = {latest}
        else:
            first = max(last + timedelta(days=1), latest - timedelta(days=CATCH_UP_DAYS - 1))
            days = {first + timedelta(days=n) for n in range((latest - first).days + 1)}
            if await self.up._run(store.get_meta, META_DAILY_REFRESHED) != latest.isoformat():
                days |= {latest - timedelta(days=n) for n in range(1, REFRESH_DAYS + 1)}
        if not days:
            return
        fetched = 0
        for day in sorted(days):
            if fetched == MAX_REQUESTS_PER_CYCLE or await self._fetch_day(day) is None:
                return
            fetched += 1
            if last is None or day > last:
                last = day
                await self.up._run(store.set_meta, META_DAILY_LAST, day.isoformat())
        await self.up._run(store.set_meta, META_DAILY_REFRESHED, latest.isoformat())

    # Statistics

    async def _async_import(self, since: int) -> None:
        """Rebuild the archive statistics from ``since`` on."""
        store = self.up.store
        energy_fields = self._energy_fields()
        for field in await self.up._run(store.archive_fields):
            rows = await self.up._run(store.archive_rows, field, since)
            energy = field in energy_fields
            unit, unit_class = self._unit(field)
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE if energy else StatisticMeanType.ARITHMETIC,
                has_sum=energy,
                name=self._name(field),
                source=DOMAIN,
                statistic_id=statistic_id(self.system_id, field),
                unit_class=unit_class,
                unit_of_measurement=unit,
            )
            if energy:
                total = await self.up._run(store.archive_energy_before, field, since)
                stats = []
                for hour, _mean, _min, _max, value in rows:
                    if value is None:
                        continue
                    total += value
                    stats.append(StatisticData(start=_utc(hour), sum=total))
            else:
                stats = [
                    StatisticData(start=_utc(hour), mean=mean, min=low, max=high)
                    for hour, mean, low, high, _energy in rows
                    if mean is not None
                ]
            if stats:
                async_add_external_statistics(self.hass, metadata, stats)

    async def _first_statistic(
        self, entity_id: str, since: int
    ) -> tuple[float, float | None] | None:
        """Return the start and sum of an entity's first hourly statistic from ``since``."""
        found = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            _utc(since),
            None,
            {entity_id},
            "hour",
            None,
            {"sum"},
        )
        rows = found.get(entity_id)
        if not rows:
            return None
        first = rows[0]
        start = first["start"]
        if isinstance(start, datetime):
            start = start.timestamp()
        return float(start), first.get("sum")

    async def _async_copy_to_sensors(self) -> None:
        """Fill download sensors' statistics with the archive before they begin.

        Only hours before the sensor's first statistic: the recorder's own
        hours are never rewritten. Energy sums are shifted so the copied
        hours join the recorder's series without a jump.
        """
        registry = er.async_get(self.hass)
        energy_fields = self._energy_fields()
        for mapping in self.up.download_mappings:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{self.system_id}_download_{mapping.field}"
            )
            if entity_id is None:
                continue
            archived = await self.up._run(self.up.store.archive_rows, mapping.field)
            if not archived:
                continue
            first = await self._first_statistic(entity_id, archived[0][0])
            boundary = first[0] if first else time.time() // 3600 * 3600
            rows = [row for row in archived if row[0] < boundary]
            if not rows:
                continue
            energy = mapping.field in energy_fields
            unit, unit_class = self._unit(mapping.field)
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE if energy else StatisticMeanType.ARITHMETIC,
                has_sum=energy,
                name=None,
                source="recorder",
                statistic_id=entity_id,
                unit_class=unit_class,
                unit_of_measurement=unit,
            )
            if energy:
                # The last copied hour ends where the recorder's series starts.
                total = 0.0
                if first and first[1] is not None:
                    following = await self.up._run(
                        self.up.store.archive_rows,
                        mapping.field,
                        int(boundary),
                        int(boundary) + 3600,
                    )
                    total = first[1] - ((following[0][4] or 0.0) if following else 0.0)
                stats = []
                for hour, _mean, _min, _max, value in reversed(rows):
                    if value is None:
                        continue
                    stats.append(StatisticData(start=_utc(hour), sum=total))
                    total -= value
                stats.reverse()
            else:
                stats = [
                    StatisticData(start=_utc(hour), mean=mean, min=low, max=high)
                    for hour, mean, low, high, _energy in rows
                    if mean is not None
                ]
            if stats:
                async_import_statistics(self.hass, metadata, stats)
                _LOGGER.debug(
                    "Copied %s hours of PVOutput history to %s", len(stats), entity_id
                )


def _utc(ts: int) -> datetime:
    return dt_util.utc_from_timestamp(ts)
