"""The PVOutput API client."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pvoutput_uploader.api import (
    BASE_URL,
    PVOutputAuthError,
    PVOutputBadRequest,
    PVOutputClient,
    PVOutputConnectionError,
    PVOutputRateLimited,
    parse_history,
    parse_status,
)

SYSTEM = (
    "My System,4500,2000,18,250,Brand,1,5000,Inverter,N,20,No,20200101,"
    "-33.8,151.2,10;;1"
)


def client(hass: HomeAssistant) -> PVOutputClient:
    return PVOutputClient(async_get_clientsession(hass), "key", "1234")


async def test_get_system(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        BASE_URL + "getsystem.jsp",
        text=SYSTEM,
        headers={
            "X-Rate-Limit-Limit": "300",
            "X-Rate-Limit-Remaining": "299",
            "X-Rate-Limit-Reset": "1800000000",
        },
    )
    c = client(hass)
    system = await c.get_system()
    assert system.name == "My System"
    assert system.interval == 10
    assert system.donor is True
    assert (c.rate_limit, c.rate_remaining, c.rate_reset) == (300, 299, 1800000000.0)

    _, _, data, headers = aioclient_mock.mock_calls[0]
    assert data == {"donations": "1"}
    assert headers["X-Pvoutput-Apikey"] == "key"
    assert headers["X-Pvoutput-SystemId"] == "1234"
    assert headers["X-Rate-Limit"] == "1"


async def test_get_system_not_donor(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(BASE_URL + "getsystem.jsp", text=SYSTEM.replace(";;1", ";;0"))
    system = await client(hass).get_system()
    assert system.donor is False


async def test_batch_status_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(BASE_URL + "addbatchstatus.jsp", text="20261010,10:05,1")
    await client(hass).add_batch_status(["20261010,10:05,5", "20261010,10:10,6"])
    assert aioclient_mock.mock_calls[0][2] == {
        "data": "20261010,10:05,5;20261010,10:10,6"
    }


async def test_rate_limited(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        BASE_URL + "addstatus.jsp",
        status=403,
        text="Forbidden 403: Exceeded 300 requests per hour",
        headers={"X-Rate-Limit-Reset": "4000000000"},
    )
    c = client(hass)
    with pytest.raises(PVOutputRateLimited) as err:
        await c.add_status({"d": "20261010", "t": "10:05", "v5": "20"})
    assert err.value.reset == 4000000000.0
    assert c.rate_remaining == 0


async def test_rate_limited_without_reset_header(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        BASE_URL + "addstatus.jsp",
        status=403,
        text="Forbidden 403: Exceeded 60 requests per hour",
    )
    with pytest.raises(PVOutputRateLimited) as err:
        await client(hass).add_status({"d": "20261010", "t": "10:05"})
    # Falls back to the top of the next hour.
    assert err.value.reset % 3600 == 0


@pytest.mark.parametrize(
    ("status", "text", "error"),
    [
        (401, "Unauthorized 401: Invalid API Key", PVOutputAuthError),
        (403, "Forbidden 403: Read only key", PVOutputAuthError),
        (400, "Bad request 400: Moon Powered", PVOutputBadRequest),
        (500, "oops", PVOutputConnectionError),
    ],
)
async def test_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    text: str,
    error: type[Exception],
) -> None:
    aioclient_mock.post(BASE_URL + "addoutput.jsp", status=status, text=text)
    with pytest.raises(error):
        await client(hass).add_output({"d": "20261001"})


async def test_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(BASE_URL + "addoutput.jsp", exc=TimeoutError())
    with pytest.raises(PVOutputConnectionError):
        await client(hass).add_batch_output(["20261001,100"])


async def test_batch_output_goes_to_addoutput(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(BASE_URL + "addoutput.jsp", text="OK 200: Added Output")
    await client(hass).add_batch_output(["20261001,100", "20261002,200"])
    method, url, data, _ = aioclient_mock.mock_calls[0]
    assert str(url) == BASE_URL + "addoutput.jsp"
    assert data == {"data": "20261001,100;20261002,200"}


def test_parse_status() -> None:
    # As getstatus.jsp answers: v1-v4, normalised output, v5, v6, then v7-v12 with ext=1.
    reading = parse_status("20261011,10:30,4210,1520,NaN,NaN,0.338,18.5,241.2,1.5,NaN,NaN,NaN,NaN,7")
    assert reading.date == "20261011"
    assert reading.time == "10:30"
    assert reading.efficiency == 0.338
    assert reading.values == {
        "v1": 4210.0, "v2": 1520.0, "v3": None, "v4": None, "v5": 18.5, "v6": 241.2,
        "v7": 1.5, "v8": None, "v9": None, "v10": None, "v11": None, "v12": 7.0,
    }
    # Without ext=1 there is no v7-v12.
    assert set(parse_status("20261011,10:30,1,2,3,4,0.1,5,6").values) == {
        "v1", "v2", "v3", "v4", "v5", "v6",
    }


async def test_get_status(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        BASE_URL + "getstatus.jsp", text="20261011,10:30,4210,1520,NaN,NaN,0.338,18.5,241.2"
    )
    reading = await client(hass).get_status(extended=True)
    assert reading.values["v2"] == 1520.0
    method, url, _, _ = aioclient_mock.mock_calls[0]
    assert method == "GET"
    assert url.query["ext"] == "1"


# Two statuses as getstatus.jsp?h=1&ext=1 answers, newest first.
HISTORY = (
    "20261010,12:10,16084,1.208,9996,9990,0.750,21796,12541,23.0,238.6,51.142,NaN,NaN,0.000,NaN,NaN;"
    "20261010,12:00,14419,1.083,9997,10002,0.751,19954,11185,22.0,237.6,53.635,NaN,NaN,0.000,NaN,NaN"
)


def test_parse_history() -> None:
    statuses = parse_history(HISTORY)
    # Oldest first.
    assert [s.time for s in statuses] == ["12:00", "12:10"]
    assert statuses[0].values == {
        "v1": 14419.0, "v2": 9997.0, "normalised": 0.751, "v3": 19954.0, "v4": 11185.0,
        "v5": 22.0, "v6": 237.6, "v7": 53.635, "v10": 0.0,
    }
    # Without ext=1 the columns end at the voltage.
    short = parse_history("20260801,18:40,21880,1.643,109,0,0.000,59820,366,NaN,234.1")
    assert short[0].values == {
        "v1": 21880.0, "v2": 109.0, "normalised": 0.0, "v3": 59820.0, "v4": 366.0, "v6": 234.1,
    }


async def test_get_status_history(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(BASE_URL + "getstatus.jsp", text=HISTORY)
    statuses = await client(hass).get_status_history("20261010", extended=True)
    assert len(statuses) == 2
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.query["h"] == "1"
    assert url.query["d"] == "20261010"
    assert url.query["limit"] == "288"
    assert url.query["ext"] == "1"

    # A day without statuses is an empty day, not an error.
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        BASE_URL + "getstatus.jsp", status=400, text="Bad request 400: No status found"
    )
    assert await client(hass).get_status_history("20250101", extended=False) == []
