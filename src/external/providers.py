"""Provider registry — the static ``DataSource`` → provider-class map.

Hardwired by design (M16-P2a): no DB config, no settings UI. Adding a
new data source (P2b PayPal, P2c Santander-CC, a future FinTS provider)
is exactly one import + one ``PROVIDERS`` entry here, plus the provider
class itself.
"""

from __future__ import annotations

from src.core.db.models import DataSource
from src.external.comdirect_provider import ComdirectProvider
from src.external.provider import BankProvider

#: The single source of truth for source → provider. Lookup is by the
#: ``DataSource`` enum value (the ``source_id`` path segment on the
#: worker's ``/internal/sync/{source_id}`` route).
PROVIDERS: dict[DataSource, type[BankProvider]] = {
    DataSource.COMDIRECT: ComdirectProvider,
}


def get_provider(source: DataSource) -> type[BankProvider]:
    """Return the provider class registered for ``source``.

    Raises :class:`ValueError` for a source with no registered provider
    — the worker maps that onto an HTTP 404.
    """
    try:
        return PROVIDERS[source]
    except KeyError:
        raise ValueError(
            f"No provider registered for source {source.value!r}"
        ) from None
