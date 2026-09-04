from __future__ import annotations

from typing import Callable

from strategies.base import Strategy
from strategies.sma_crossover import SmaCrossover

_REGISTRY: dict[str, Callable[..., Strategy]] = {
    SmaCrossover.name: SmaCrossover,
}


def list_strategies() -> list[str]:
    return sorted(_REGISTRY)


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    try:
        factory = _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy {name!r}; available: {list_strategies()}") from exc
    return factory(**(params or {}))
