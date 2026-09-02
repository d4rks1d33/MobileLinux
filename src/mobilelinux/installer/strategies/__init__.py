"""Install/flash strategy registry."""

from __future__ import annotations

from ...core.errors import StrategyError
from .base import Strategy
from .rescue_dd import RescueDdStrategy
from .simple import (
    AdbShellDdStrategy,
    FastbootStrategy,
    FastbootdStrategy,
    HeimdallStrategy,
    SdcardStrategy,
    UuuStrategy,
)

_STRATEGIES: dict[str, type[Strategy]] = {
    RescueDdStrategy.name: RescueDdStrategy,
    FastbootStrategy.name: FastbootStrategy,
    FastbootdStrategy.name: FastbootdStrategy,
    HeimdallStrategy.name: HeimdallStrategy,
    SdcardStrategy.name: SdcardStrategy,
    UuuStrategy.name: UuuStrategy,
    AdbShellDdStrategy.name: AdbShellDdStrategy,
}


def get_strategy(name: str) -> type[Strategy]:
    cls = _STRATEGIES.get(name)
    if cls is None:
        raise StrategyError(
            f"no strategy implementation for '{name}'. "
            f"Known: {', '.join(sorted(_STRATEGIES))}"
        )
    return cls


def known_strategies() -> list[str]:
    return sorted(_STRATEGIES)
