"""Own lazy parser dependencies and connect them to Core's refresh callback."""

from contextlib import ExitStack

from valutatrade_hub.core.currencies import CryptoCurrency, get_currency
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_parser_logging
from valutatrade_hub.parser_service.api_clients import (
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RateStorage
from valutatrade_hub.parser_service.updater import RatesUpdater, UpdateResult


class ParserRuntime(ExitStack):
    """Create clients only on demand and close owned HTTP sessions on exit."""

    def __init__(self, settings: SettingsLoader, database: DatabaseManager) -> None:
        super().__init__()
        self._settings = settings
        self._database = database
        self._updater = None

    def update(self, source: str | None = None) -> UpdateResult:
        """Explicit updates always attempt the requested providers once."""
        if self._updater is None:
            config = ParserConfig.from_settings(self._settings)
            configure_parser_logging(self._settings)
            clients = {}
            for name, client_type in (
                ("coingecko", CoinGeckoClient),
                ("exchangerate", ExchangeRateApiClient),
            ):
                client = client_type(config)
                self.callback(client.close)
                clients[name] = client
            self._updater = RatesUpdater(RateStorage(self._database), clients)
        return self._updater.run_update(source)

    def refresh(self, currencies: frozenset[str]) -> None:
        """Request only providers needed by the unavailable currency legs."""
        sources = {
            "coingecko"
            if isinstance(get_currency(code), CryptoCurrency)
            else "exchangerate"
            for code in currencies
            if code != "USD"
        }
        if sources:
            self.update(next(iter(sources)) if len(sources) == 1 else None)
