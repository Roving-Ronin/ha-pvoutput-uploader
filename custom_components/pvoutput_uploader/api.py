"""Minimal PVOutput r2 API client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import time

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://pvoutput.org/service/r2/"
TIMEOUT = aiohttp.ClientTimeout(total=30)


class PVOutputError(Exception):
    """A PVOutput request failed."""


class PVOutputConnectionError(PVOutputError):
    """PVOutput could not be reached or answered with a server error."""


class PVOutputAuthError(PVOutputError):
    """The API key or system ID was refused."""


class PVOutputBadRequest(PVOutputError):
    """PVOutput rejected the data (HTTP 400)."""


class PVOutputRateLimited(PVOutputError):
    """The hourly request limit is used up."""

    def __init__(self, message: str, reset: float) -> None:
        """Initialize with the time the limit resets."""
        super().__init__(message)
        self.reset = reset


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """What getsystem.jsp tells us about the system."""

    name: str
    interval: int
    donor: bool


@dataclass(frozen=True, slots=True)
class StatusReading:
    """The latest status PVOutput holds for the system (getstatus.jsp)."""

    date: str  # yyyymmdd
    time: str  # hh:mm
    values: dict[str, float | None]  # v1..v12; None where PVOutput has none
    efficiency: float | None  # normalised output, kWh/kW


# getstatus.jsp columns after date and time; v7-v12 only with ext=1 (donors).
STATUS_COLUMNS = ("v1", "v2", "v3", "v4", "efficiency", "v5", "v6") + tuple(
    f"v{n}" for n in range(7, 13)
)


def _number(text: str) -> float | None:
    """Return a status value; PVOutput sends NaN for none."""
    try:
        value = float(text)
    except ValueError:
        return None
    return None if value != value else value  # NaN


def parse_status(text: str) -> StatusReading:
    """Parse a getstatus.jsp answer (no history)."""
    cols = text.strip().split(",")
    if len(cols) < 3:
        raise PVOutputError(f"getstatus.jsp: unexpected answer {text[:80]!r}")
    found = dict(zip(STATUS_COLUMNS, cols[2:], strict=False))
    values = {
        key: _number(found[key]) for key in STATUS_COLUMNS if key != "efficiency" and key in found
    }
    efficiency = _number(found["efficiency"]) if "efficiency" in found else None
    return StatusReading(cols[0], cols[1], values, efficiency)


class PVOutputClient:
    """Talks to PVOutput and tracks the hourly rate limit."""

    def __init__(
        self, session: aiohttp.ClientSession, api_key: str, system_id: str
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._headers = {
            "X-Pvoutput-Apikey": api_key,
            "X-Pvoutput-SystemId": str(system_id),
            "X-Rate-Limit": "1",
        }
        self.rate_limit: int | None = None
        self.rate_remaining: int | None = None
        self.rate_reset: float | None = None

    def _read_rate_headers(self, headers: Mapping[str, str]) -> None:
        try:
            if (limit := headers.get("X-Rate-Limit-Limit")) is not None:
                self.rate_limit = int(limit)
            if (remaining := headers.get("X-Rate-Limit-Remaining")) is not None:
                self.rate_remaining = int(remaining)
            if (reset := headers.get("X-Rate-Limit-Reset")) is not None:
                self.rate_reset = float(reset)
        except ValueError:
            pass

    async def _request(self, method: str, service: str, data: dict[str, str]) -> str:
        url = BASE_URL + service
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                data=data if method == "POST" else None,
                params=data if method == "GET" else None,
                timeout=TIMEOUT,
            ) as resp:
                text = (await resp.text()).strip()
                self._read_rate_headers(resp.headers)
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PVOutputConnectionError(f"{service}: {err}") from err

        _LOGGER.debug("%s -> %s %s", service, status, text[:200])
        if status == 200:
            return text
        if status == 403 and "exceeded" in text.lower():
            reset = self.rate_reset
            if reset is None or reset <= time.time():
                reset = (time.time() // 3600 + 1) * 3600
            self.rate_remaining = 0
            raise PVOutputRateLimited(text, reset)
        if status in (401, 403):
            raise PVOutputAuthError(text)
        if status == 400:
            raise PVOutputBadRequest(text)
        raise PVOutputConnectionError(f"{service}: HTTP {status} {text}")

    async def get_system(self) -> SystemInfo:
        """Return the system name, status interval and donation state."""
        text = await self._request("POST", "getsystem.jsp", {"donations": "1"})
        sections = text.split(";")
        fields = sections[0].split(",")
        try:
            interval = int(fields[15])
        except (IndexError, ValueError):
            interval = 5
        donor = False
        if len(sections) > 1:
            try:
                donor = int(sections[-1].split(",")[0]) > 0
            except ValueError:
                donor = False
        return SystemInfo(fields[0], interval, donor)

    async def get_status(self, extended: bool) -> StatusReading:
        """Return the latest status; ``extended`` adds v7-v12 (donors only).

        Battery fields are not returned by getstatus.jsp.
        """
        params = {"ext": "1"} if extended else {}
        return parse_status(await self._request("GET", "getstatus.jsp", params))

    async def add_status(self, params: dict[str, str]) -> str:
        """Send one status, including battery values."""
        return await self._request("POST", "addstatus.jsp", params)

    async def add_batch_status(self, lines: list[str]) -> str:
        """Send up to 30 (100 for donors) statuses."""
        return await self._request("POST", "addbatchstatus.jsp", {"data": ";".join(lines)})

    async def add_output(self, params: dict[str, str]) -> str:
        """Send one daily output."""
        return await self._request("POST", "addoutput.jsp", params)

    async def add_batch_output(self, lines: list[str]) -> str:
        """Send up to 100 daily outputs in one request (donors only).

        Batches go to addoutput.jsp too, as a ``data`` parameter.
        """
        return await self._request("POST", "addoutput.jsp", {"data": ";".join(lines)})
