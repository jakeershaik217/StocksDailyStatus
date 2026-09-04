from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

import requests

from strategies.base import Bar, Signal

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are a risk reviewer for an automated equity strategy on Indian markets. "
    "You receive a rule-based signal and recent price bars. You may VETO a signal "
    "if the context looks unsafe (e.g. abnormal gap, thin volume, signal contradicts "
    "trend), otherwise let it pass. You never create trades. Respond with JSON only: "
    '{"veto": bool, "confidence": 0-1, "rationale": "<one sentence>"}'
)


@dataclass(frozen=True)
class AiReview:
    veto: bool
    confidence: Decimal
    rationale: str
    model: str


class SignalReviewer:
    """Optional LLM second opinion. Deterministic rules decide; the LLM can only veto.

    Disabled (returns None) when no API key is configured, so the runner works
    without any AI dependency.
    """

    def __init__(self, api_key: str | None, model: str = "gpt-4o-mini", timeout: int = 30) -> None:
        self.api_key = api_key or None
        self.model = model
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return self.api_key is not None

    def review(self, signal: Signal, bars: Sequence[Bar]) -> AiReview | None:
        if not self.enabled:
            return None
        recent = [
            {"t": b.timestamp.isoformat(), "o": str(b.open), "h": str(b.high), "l": str(b.low), "c": str(b.close), "v": b.volume}
            for b in bars[-20:]
        ]
        user = {
            "signal": {
                "symbol": signal.symbol,
                "side": signal.side,
                "strategy": signal.strategy,
                "reason": signal.reason,
                "strength": str(signal.strength),
                "reference_price": str(signal.reference_price),
                "metadata": signal.metadata,
            },
            "recent_bars": recent,
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user)},
            ],
        }
        resp = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return parse_review(content, self.model)


def parse_review(content: str, model: str) -> AiReview:
    try:
        data = json.loads(content)
        return AiReview(
            veto=bool(data.get("veto", False)),
            confidence=Decimal(str(data.get("confidence", 0))),
            rationale=str(data.get("rationale", "")),
            model=model,
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        # Unparseable output fails closed: treat as veto so a broken model never lets trades through.
        return AiReview(veto=True, confidence=Decimal(0), rationale=f"unparseable model output: {content[:120]}", model=model)
