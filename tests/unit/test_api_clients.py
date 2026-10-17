"""Offline provider contract tests using saved, synthetic response examples."""

import json
import traceback
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging
from valutatrade_hub.parser_service.api_clients import (
    BaseApiClient,
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig

FIXTURES = Path(__file__).parents[1] / "fixtures" / "api"
NOW = datetime(2026, 10, 18, 12, 30, tzinfo=UTC)
PROVIDERS = [CoinGeckoClient, ExchangeRateApiClient]


def document(name):
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))


def successful_payload(client_type):
    return document(
        "coingecko_success"
        if client_type is CoinGeckoClient
        else "exchangerate_success"
    )


def build_client(
    client_type, payload=None, *, status=200, config=None, clock=None, timer=None
):
    response = Mock(spec=requests.Response)
    response.status_code = status
    response.json.return_value = (
        successful_payload(client_type) if payload is None else payload
    )
    session = Mock(spec=requests.Session)
    session.get.return_value = response
    client = client_type(
        config
        or ParserConfig(
            exchangerate_api_key="private-exchange-key",
            coingecko_api_key="private-demo-key",
        ),
        session=session,
        clock=clock or (lambda: NOW),
        timer=timer or Mock(side_effect=[1.0, 1.125]),
    )
    return client, session, response


def test_base_client_is_abstract():
    with pytest.raises(TypeError):
        BaseApiClient()


def test_coingecko_request_and_normalized_rates_with_provider_metadata():
    client, session, response = build_client(CoinGeckoClient)
    rates = client.fetch_rates()
    assert rates == {"BTC_USD": 59337.21, "ETH_USD": 3720, "SOL_USD": 145.12}
    session.get.assert_called_once_with(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": "bitcoin,ethereum,solana",
            "vs_currencies": "usd",
            "include_last_updated_at": "true",
        },
        headers={"x-cg-demo-api-key": "private-demo-key"},
        timeout=10,
        allow_redirects=False,
    )
    assert "private-demo-key" not in session.get.call_args.args[0]
    metadata = client.last_metadata
    assert metadata.received_at == NOW
    assert metadata.request_ms == 125
    assert metadata.status_code == 200
    assert metadata.pair_metadata["BTC_USD"]["raw_id"] == "bitcoin"
    expected = (
        datetime.fromtimestamp(1792238400, UTC).isoformat().replace("+00:00", "Z")
    )
    assert metadata.pair_metadata["BTC_USD"]["provider_updated_at"] == expected
    assert metadata.pair_metadata["BTC_USD"] != metadata.pair_metadata["ETH_USD"]
    response.close.assert_called_once()


def test_exchangerate_request_and_reciprocal_direction():
    client, session, response = build_client(ExchangeRateApiClient)
    rates = client.fetch_rates()
    assert rates == pytest.approx(
        {"EUR_USD": 1 / 0.927, "GBP_USD": 1 / 0.809, "RUB_USD": 1 / 98.45}
    )
    assert rates["EUR_USD"] != 0.927
    assert "USD_USD" not in rates
    session.get.assert_called_once_with(
        "https://v6.exchangerate-api.com/v6/private-exchange-key/latest/USD",
        params={},
        headers={},
        timeout=10,
        allow_redirects=False,
    )
    assert client.source == "ExchangeRate-API"
    assert client.last_metadata.received_at == NOW
    assert len(client.last_metadata.pair_metadata) == 3
    assert all(
        "provider_updated_at" in metadata
        for metadata in client.last_metadata.pair_metadata.values()
    )


def test_subset_timeout_and_optional_coingecko_key():
    config = ParserConfig(
        coingecko_api_key=None, crypto_currencies=("SOL",), request_timeout_seconds=4
    )
    client, session, response = build_client(CoinGeckoClient, config=config)
    assert client.fetch_rates() == {"SOL_USD": 145.12}
    assert session.get.call_args.kwargs["params"]["ids"] == "solana"
    assert session.get.call_args.kwargs["headers"] == {}
    assert session.get.call_args.kwargs["timeout"] == 4


def test_missing_exchange_key_makes_no_request_and_does_not_block_coingecko():
    config = ParserConfig(exchangerate_api_key=None, coingecko_api_key=None)
    fiat, session, response = build_client(ExchangeRateApiClient, config=config)
    with pytest.raises(ApiRequestError, match="не задан EXCHANGERATE_API_KEY"):
        fiat.fetch_rates()
    session.get.assert_not_called()
    assert fiat.last_metadata is None
    crypto, session, response = build_client(CoinGeckoClient, config=config)
    assert crypto.fetch_rates()["BTC_USD"] == 59337.21


@pytest.mark.parametrize("client_type", PROVIDERS)
@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 502, 503])
def test_http_failures_do_not_parse_body_retry_or_publish_metadata(client_type, status):
    client, session, response = build_client(client_type, status=status)
    with pytest.raises(ApiRequestError, match=f"HTTP {status}"):
        client.fetch_rates()
    response.json.assert_not_called()
    response.close.assert_called_once()
    session.get.assert_called_once()
    assert client.last_metadata is None


@pytest.mark.parametrize("client_type", PROVIDERS)
@pytest.mark.parametrize(
    "error_type",
    [
        requests.Timeout,
        requests.ConnectionError,
        requests.HTTPError,
        requests.RequestException,
    ],
)
def test_transport_failures_suppress_secret_exception_details(client_type, error_type):
    client, session, response = build_client(client_type)
    session.get.side_effect = error_type(
        "https://example.test/private-exchange-key?key=private-demo-key"
    )
    with pytest.raises(ApiRequestError) as captured:
        client.fetch_rates()
    message = "".join(traceback.format_exception(captured.value))
    assert "private-exchange-key" not in message
    assert "private-demo-key" not in message
    assert "https://example.test" not in message
    assert captured.value.__suppress_context__
    assert client.last_metadata is None
    session.get.assert_called_once()


@pytest.mark.parametrize("client_type", PROVIDERS)
def test_invalid_json_is_safe_and_response_is_closed(client_type):
    client, session, response = build_client(client_type)
    response.json.side_effect = requests.exceptions.JSONDecodeError(
        "private-exchange-key", "secret", 0
    )
    with pytest.raises(ApiRequestError, match="JSON") as captured:
        client.fetch_rates()
    assert "private" not in "".join(traceback.format_exception(captured.value))
    assert client.last_metadata is None
    response.close.assert_called_once()


@pytest.mark.parametrize("client_type", PROVIDERS)
@pytest.mark.parametrize("payload", [[], "text", {}, {"unexpected": "private-key"}])
def test_incorrect_root_or_missing_required_fields_fail_atomically(
    client_type, payload
):
    client, session, response = build_client(client_type, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


@pytest.mark.parametrize("client_type", PROVIDERS)
@pytest.mark.parametrize(
    "rate",
    [
        None,
        True,
        False,
        0,
        -1,
        "1.5",
        float("nan"),
        float("inf"),
        float("-inf"),
        10**400,
    ],
)
def test_invalid_requested_rate_discards_entire_response(client_type, rate):
    payload = successful_payload(client_type)
    if client_type is CoinGeckoClient:
        payload["solana"]["usd"] = rate
    else:
        payload["conversion_rates"]["RUB"] = rate
    client, session, response = build_client(client_type, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


@pytest.mark.parametrize("client_type", PROVIDERS)
def test_incomplete_payload_is_not_accepted_as_partial_success(client_type):
    payload = successful_payload(client_type)
    if client_type is CoinGeckoClient:
        del payload["solana"]
    else:
        del payload["conversion_rates"]["RUB"]
    client, session, response = build_client(client_type, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


@pytest.mark.parametrize("replacement", ["EUR", None, True])
def test_exchangerate_rejects_mismatched_response_base(replacement):
    payload = successful_payload(ExchangeRateApiClient)
    payload["base_code"] = replacement
    client, session, response = build_client(ExchangeRateApiClient, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()


def test_legacy_rates_field_is_not_a_fallback_for_conversion_rates():
    payload = successful_payload(ExchangeRateApiClient)
    payload["rates"] = payload.pop("conversion_rates")
    client, session, response = build_client(ExchangeRateApiClient, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()


def test_reciprocal_overflow_is_not_a_successful_quote():
    payload = successful_payload(ExchangeRateApiClient)
    payload["conversion_rates"]["EUR"] = 5e-324
    client, session, response = build_client(ExchangeRateApiClient, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


@pytest.mark.parametrize("client_type", PROVIDERS)
def test_http_200_api_error_is_rejected(client_type):
    payload = document(
        "coingecko_error" if client_type is CoinGeckoClient else "exchangerate_error"
    )
    client, session, response = build_client(client_type, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


@pytest.mark.parametrize(
    "error_type",
    [
        "invalid-key",
        "inactive-account",
        "quota-reached",
        "unsupported-code",
        "malformed-request",
        "private-key",
        [],
        None,
    ],
)
def test_exchangerate_error_types_have_safe_diagnostics(error_type):
    client, session, response = build_client(
        ExchangeRateApiClient, {"result": "error", "error-type": error_type}
    )
    with pytest.raises(ApiRequestError) as captured:
        client.fetch_rates()
    assert "private-key" not in str(captured.value)


@pytest.mark.parametrize("client_type", PROVIDERS)
@pytest.mark.parametrize("failure", ["transport", "http", "json", "payload"])
def test_a_failed_attempt_clears_previous_successful_metadata(client_type, failure):
    client, session, response = build_client(client_type, timer=lambda: 1.0)
    client.fetch_rates()
    assert client.last_metadata is not None
    if failure == "transport":
        session.get.side_effect = requests.Timeout("private-key")
    elif failure == "http":
        response.status_code = 429
    elif failure == "json":
        response.json.side_effect = ValueError("private-key")
    else:
        response.json.return_value = {}
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


@pytest.mark.parametrize("client_type", PROVIDERS)
@pytest.mark.parametrize(
    "timestamp", [None, True, 0, -1, "1792238400", float("nan"), 1e300]
)
def test_invalid_upstream_timestamp_discards_the_response(client_type, timestamp):
    payload = successful_payload(client_type)
    if client_type is CoinGeckoClient:
        payload["bitcoin"]["last_updated_at"] = timestamp
    else:
        payload["time_last_update_unix"] = timestamp
    client, session, response = build_client(client_type, payload)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


@pytest.mark.parametrize("client_type", PROVIDERS)
def test_absent_optional_provider_time_does_not_fabricate_metadata(client_type):
    payload = successful_payload(client_type)
    if client_type is CoinGeckoClient:
        for record in payload.values():
            del record["last_updated_at"]
    else:
        del payload["time_last_update_unix"]
    client, session, response = build_client(client_type, payload)
    client.fetch_rates()
    assert client.last_metadata.received_at == NOW
    assert all(
        "provider_updated_at" not in metadata
        for metadata in client.last_metadata.pair_metadata.values()
    )


def test_receipt_time_is_normalized_to_utc_and_distinct_from_provider_time():
    local_time = NOW.astimezone(timezone(timedelta(hours=3)))
    client, session, response = build_client(CoinGeckoClient, clock=lambda: local_time)
    client.fetch_rates()
    assert client.last_metadata.received_at == NOW
    assert client.last_metadata.received_at.tzinfo is UTC


@pytest.mark.parametrize("clock", [lambda: datetime(2026, 10, 18), lambda: None])
def test_invalid_injected_clock_cannot_create_metadata(clock):
    client, session, response = build_client(CoinGeckoClient, clock=clock)
    with pytest.raises(ApiRequestError):
        client.fetch_rates()
    assert client.last_metadata is None


def test_network_guard_blocks_unmocked_requests():
    with pytest.raises(AssertionError, match="Реальные HTTP-запросы запрещены"):
        requests.get("https://example.test")


def test_client_error_is_safe_when_logged_by_a_trading_operation(database):
    configure_logging(SettingsLoader())
    client, session, response = build_client(ExchangeRateApiClient)
    session.get.side_effect = requests.ConnectionError(
        "https://example.test/private-exchange-key"
    )
    service = TradingService(
        database, rate_provider=lambda source, target: client.fetch_rates()["EUR_USD"]
    )
    service.register("Алиса", "private-password")
    service.login("Алиса", "private-password")
    with pytest.raises(ApiRequestError):
        service.buy("EUR", 1)
    text = (SettingsLoader().get("log_dir") / "actions.log").read_text("utf-8")
    assert "private-exchange-key" not in text
    assert "private-password" not in text
    assert "https://" not in text
    assert json.loads(text)["error_type"] == "ApiRequestError"


def test_injected_session_is_not_owned_and_owned_session_can_be_closed(monkeypatch):
    client, session, response = build_client(CoinGeckoClient)
    client.close()
    session.close.assert_not_called()
    owned = Mock(spec=requests.Session)
    monkeypatch.setattr(requests, "Session", Mock(return_value=owned))
    client = CoinGeckoClient(ParserConfig())
    client.close()
    owned.close.assert_called_once()
