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
    aioclient_mock.post(BASE_URL + "addbatchoutput.jsp", exc=TimeoutError())
    with pytest.raises(PVOutputConnectionError):
        await client(hass).add_batch_output(["20261001,100"])
