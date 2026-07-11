"""Focused tests for the one-shot Larapaper client contract."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.larapaper_bridge import client


class FakeResponse:
    def __init__(self, status: int, body: object = None, *, error: Exception | None = None) -> None:
        self.status = status
        self._body = body
        self._error = error
        self.closed = False

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def json(self, **_kwargs: object) -> object:
        if self._error:
            raise self._error
        return self._body



class HangingBodyResponse(FakeResponse):
    async def json(self, **_kwargs: object) -> object:
        await asyncio.Event().wait()
        return None

class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.response


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> tuple[client.LarapaperClient, FakeSession]:
    session = FakeSession(FakeResponse(200, {}))
    monkeypatch.setattr(client, "async_get_clientsession", lambda _hass: session)
    return client.LarapaperClient(object(), "https://host/bridge/", 60), session


@pytest.mark.asyncio
async def test_setup_uses_prefixed_url_and_only_id_header(
    fake_client: tuple[client.LarapaperClient, FakeSession],
) -> None:
    integration, session = fake_client
    session.response._body = {"api_key": " key ", "friendly_id": " friendly ", "ignored": "x"}

    result = await integration.async_setup("AA:BB:CC:DD:EE:FF")

    assert len(session.calls) == 1
    url, request = session.calls[0]
    assert url == "https://host/bridge/api/setup"
    assert request["headers"] == {"ID": "AA:BB:CC:DD:EE:FF"}
    assert request["allow_redirects"] is False
    assert request["timeout"].total == client.SETUP_TIMEOUT_SECONDS
    assert session.response.closed


@pytest.mark.asyncio
async def test_display_normalizes_image_and_clamps_interval(
    fake_client: tuple[client.LarapaperClient, FakeSession],
) -> None:
    integration, session = fake_client
    session.response._body = {"refresh_rate": 15, "image_url": "/image.png"}

    result = await integration.async_display("AA:BB:CC:DD:EE:FF", "secret")

    assert result == client.DisplayResult("/image.png", 60)
    assert session.calls[0][0] == "https://host/bridge/api/display"
    assert session.calls[0][1]["headers"] == {
        "ID": "AA:BB:CC:DD:EE:FF",
        "Access-Token": "secret",
    }
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"refresh_rate": 15},
        {"refresh_rate": 15, "image_url": None},
        {"refresh_rate": 15, "image_url": ""},
    ],
)
async def test_display_normalizes_missing_or_empty_image_url(
    fake_client: tuple[client.LarapaperClient, FakeSession],
    body: dict[str, object],
) -> None:
    integration, session = fake_client
    session.response._body = body

    result = await integration.async_display("AA:BB:CC:DD:EE:FF", "secret")

    assert result == client.DisplayResult(None, 60)
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"refresh_rate": 0},
        {"refresh_rate": float("nan")},
        {"refresh_rate": 10**1000},
        {"refresh_rate": 1, "image_url": 4},
    ],
)
async def test_display_rejects_invalid_response(
    fake_client: tuple[client.LarapaperClient, FakeSession], body: object,
) -> None:
    integration, session = fake_client
    session.response._body = body

    with pytest.raises(client.LarapaperClientError) as raised:
        await integration.async_display("AA:BB:CC:DD:EE:FF", "secret")

    assert raised.value.code == "invalid_display_response"


@pytest.mark.asyncio
async def test_setup_404_is_actionable_and_non_success_is_safe(
    fake_client: tuple[client.LarapaperClient, FakeSession],
) -> None:
    integration, session = fake_client
    session.response.status = 404

    with pytest.raises(client.LarapaperClientError) as raised:
        await integration.async_setup("AA:BB:CC:DD:EE:FF")

    assert raised.value.code == "setup_auto_assign_disabled"
    assert "assign_new_devices" in str(raised.value)
    assert "secret" not in str(raised.value)

    session.response.status = 500
    with pytest.raises(client.LarapaperClientError) as raised:
        await integration.async_setup("AA:BB:CC:DD:EE:FF")
    assert raised.value.code == "setup_failed"


@pytest.mark.asyncio
async def test_timeout_covers_hanging_body(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(HangingBodyResponse(200))
    monkeypatch.setattr(client, "async_get_clientsession", lambda _hass: session)
    monkeypatch.setattr(client, "SETUP_TIMEOUT_SECONDS", 0.01)
    integration = client.LarapaperClient(object(), "https://host/", 60)

    with pytest.raises(client.LarapaperClientError) as raised:
        await integration.async_setup("AA:BB:CC:DD:EE:FF")

    assert raised.value.code == "setup_failed"
    assert session.response.closed


@pytest.mark.asyncio
async def test_timeout_covers_stalled_request(monkeypatch: pytest.MonkeyPatch) -> None:
    class StalledSession:
        async def get(self, *_args: object, **_kwargs: object) -> object:
            await asyncio.Event().wait()
            return None

    monkeypatch.setattr(client, "async_get_clientsession", lambda _hass: StalledSession())
    monkeypatch.setattr(client, "DISPLAY_TIMEOUT_SECONDS", 0.01)
    integration = client.LarapaperClient(object(), "https://host/", 60)

    with pytest.raises(client.LarapaperClientError) as raised:
        await integration.async_display("AA:BB:CC:DD:EE:FF", "secret")

    assert raised.value.code == "display_failed"


@pytest.mark.asyncio
async def test_cancellation_is_not_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    class CancelSession:
        async def get(self, *_args: object, **_kwargs: object) -> object:
            raise asyncio.CancelledError

    monkeypatch.setattr(client, "async_get_clientsession", lambda _hass: CancelSession())
    integration = client.LarapaperClient(object(), "https://host/", 60)

    with pytest.raises(asyncio.CancelledError):
        await integration.async_display("AA:BB:CC:DD:EE:FF", "secret")

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "error"),
    [("not-an-object", None), (None, ValueError("invalid json"))],
)
async def test_setup_rejects_malformed_responses(
    fake_client: tuple[client.LarapaperClient, FakeSession],
    body: object,
    error: Exception | None,
) -> None:
    integration, session = fake_client
    session.response = FakeResponse(200, body, error=error)

    with pytest.raises(client.LarapaperClientError) as raised:
        await integration.async_setup("AA:BB:CC:DD:EE:FF")

    assert raised.value.code == "setup_failed"
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (302, {}, None),
        (500, {}, None),
        (200, None, ValueError("invalid json")),
    ],
)
async def test_display_failures_are_safe_and_single_attempt(
    fake_client: tuple[client.LarapaperClient, FakeSession],
    status: int,
    body: object,
    error: Exception | None,
) -> None:
    integration, session = fake_client
    session.response = FakeResponse(status, body, error=error)

    with pytest.raises(client.LarapaperClientError) as raised:
        await integration.async_display("AA:BB:CC:DD:EE:FF", "secret")

    assert raised.value.code == "display_failed"
    assert len(session.calls) == 1
    assert "secret" not in str(raised.value)
