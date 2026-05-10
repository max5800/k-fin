"""Tests for two-step auth in ComdirectClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.external.comdirect_client import ComdirectClient


def _mock_response(status_code, json_data=None, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    return resp


@pytest.fixture
def client():
    with patch("src.external.comdirect_client.settings") as mock_settings:
        mock_settings.comdirect_client_id = "test-id"
        mock_settings.comdirect_client_secret = "test-secret"
        mock_settings.comdirect_username = "john.doe"
        mock_settings.comdirect_pin = "12345"
        mock_settings.comdirect_tan_method = "pushTAN"
        yield ComdirectClient()


@pytest.mark.asyncio
async def test_begin_auth_returns_state(client):
    """begin_auth() runs steps 1-3 and returns session/challenge IDs."""
    step1_resp = _mock_response(200, {"access_token": "primary-tok"})
    step2_resp = _mock_response(200, [{"identifier": "sess-123"}])
    step3_resp = _mock_response(
        201,
        headers={"x-once-authentication-info": '{"id": "challenge-1", "typ": "pushTAN"}'},
    )

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(side_effect=[step1_resp, step3_resp])
    mock_http.get = AsyncMock(return_value=step2_resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.external.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.begin_auth()

    assert result == {"session_identifier": "sess-123", "challenge_id": "challenge-1"}
    assert client._primary_token == "primary-tok"


@pytest.mark.asyncio
async def test_begin_auth_step1_failure_raises(client):
    """begin_auth() raises RuntimeError if step 1 fails."""
    step1_resp = _mock_response(401)

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=step1_resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.external.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        with pytest.raises(RuntimeError, match="Step 1 failed"):
            await client.begin_auth()


@pytest.mark.asyncio
async def test_complete_auth_succeeds(client):
    """complete_auth() activates session and gets secondary token."""
    # Pre-set primary token (would be set by begin_auth)
    client._primary_token = "primary-tok"
    client.access_token = "primary-tok"

    step5_resp = _mock_response(200)
    step6_resp = _mock_response(200, {"access_token": "secondary-tok"})

    mock_http = AsyncMock()
    mock_http.patch = AsyncMock(return_value=step5_resp)
    mock_http.post = AsyncMock(return_value=step6_resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.external.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.complete_auth("sess-123", "challenge-1")

    assert result is True
    assert client._secondary_token == "secondary-tok"
    assert client.is_authenticated


@pytest.mark.asyncio
async def test_complete_auth_activation_fails(client):
    """complete_auth() returns False if session activation fails."""
    client._primary_token = "primary-tok"
    client.access_token = "primary-tok"

    step5_resp = _mock_response(403)

    mock_http = AsyncMock()
    mock_http.patch = AsyncMock(return_value=step5_resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.external.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.complete_auth("sess-123", "challenge-1")

    assert result is False
    assert not client.is_authenticated


@pytest.mark.asyncio
async def test_complete_auth_token_fails(client):
    """complete_auth() returns False if secondary token request fails."""
    client._primary_token = "primary-tok"
    client.access_token = "primary-tok"

    step5_resp = _mock_response(200)
    step6_resp = _mock_response(500)

    mock_http = AsyncMock()
    mock_http.patch = AsyncMock(return_value=step5_resp)
    mock_http.post = AsyncMock(return_value=step6_resp)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    with patch("src.external.comdirect_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.complete_auth("sess-123", "challenge-1")

    assert result is False
