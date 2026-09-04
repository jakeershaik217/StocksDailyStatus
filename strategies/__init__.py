"""Rule-based trading strategies that emit Signals from price Bars."""

from strategies.base import Bar, Signal, Strategy
from strategies.registry import build_strategy, list_strategies

__all__ = ["Bar", "Signal", "Strategy", "build_strategy", "list_strategies"]
