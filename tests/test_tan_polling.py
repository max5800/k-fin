"""Tests for TAN polling in ComdirectClient.authenticate_full()."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.connector.comdirect_client import ComdirectClient, ComdirectTANTimeoutError


def _mock_response(status_code, json_data=None, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    return resp


@pytest.fixture
def client():
    with patch("src.connector.comdirect_client.settings") as mock_settings:
        mock_settings.comdirect_client_id = "test-id"
        mock_settings.comdirect_client_secret = "test-secret"
        mock_settings.comdirect_username = "john.doe"
        mock_settings.comdirect_pin = "12345"
        mock_settings.comdirect_tan_method = "pushTAN"
        mock_settings.comdirect_tan_poll_interval_s = 0.01  # fast for tests
        mock_settings.comdirect_tan_timeout_s = 0.05  # short timeout for tests
        yield ComdirectClient()


@pytest.mark.asyncio
async def test_tan_polling_succeeds_after_retries(client):
    """TAN poll returns 403 twice, then 200 — auth succeeds."""
    step1_resp = _mock_response(200, {"access_token": "primary-tok"})
    step2_resp = _mock_response(200, [{"identifier": "sess-123"}])
    step3_resp = _mock_response(
        201,
        headers={"x-once-authentication-info": '{"id": "challenge-1", "typ": "pushTAN"}'},
    )
    poll_403 = _mock_response(403)
    poll_200 = _mock_response(200)
    step6_resp = _mock_response(200, {"access_token": "secondary-tok"})

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(side_effect=[step1_resp, step3_resp, step6_resp])
    mock_http.get = AsyncMock(return_value=step2_resp)
    mock_http.patch = AsyncMock(side_effect=[poll_403, poll_403, poll_200])
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.connector.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.authenticate_full()

    assert result is True
    assert client._secondary_token == "secondary-tok"
    assert mock_http.patch.call_count == 3


@pytest.mark.asyncio
async def test_tan_polling_timeout_raises(client):
    """TAN poll never returns 200 — should raise ComdirectTANTimeoutError."""
    step1_resp = _mock_response(200, {"access_token": "primary-tok"})
    step2_resp = _mock_response(200, [{"identifier": "sess-123"}])
    step3_resp = _mock_response(
        201,
        headers={"x-once-authentication-info": '{"id": "challenge-1", "typ": "pushTAN"}'},
    )
    poll_403 = _mock_response(403)

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(side_effect=[step1_resp, step3_resp])
    mock_http.get = AsyncMock(return_value=step2_resp)
    mock_http.patch = AsyncMock(return_value=poll_403)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.connector.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        with pytest.raises(ComdirectTANTimeoutError, match="not confirmed"):
            await client.authenticate_full()


@pytest.mark.asyncio
async def test_tan_polling_immediate_success(client):
    """TAN poll returns 200 on first try — no retries needed."""
    step1_resp = _mock_response(200, {"access_token": "primary-tok"})
    step2_resp = _mock_response(200, [{"identifier": "sess-123"}])
    step3_resp = _mock_response(
        201,
        headers={"x-once-authentication-info": '{"id": "challenge-1", "typ": "pushTAN"}'},
    )
    poll_200 = _mock_response(200)
    step6_resp = _mock_response(200, {"access_token": "secondary-tok"})

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(side_effect=[step1_resp, step3_resp, step6_resp])
    mock_http.get = AsyncMock(return_value=step2_resp)
    mock_http.patch = AsyncMock(return_value=poll_200)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.connector.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.authenticate_full()

    assert result is True
    assert mock_http.patch.call_count == 1
