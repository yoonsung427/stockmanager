"""Builders for ``ReasonerOutput`` (the Reasoner's *intent*): hold, a sell+buy pair, a single buy."""

from __future__ import annotations

from typing import Any

from stockmanager.evaluation.reasoner_io import ReasonerOutput

_DIMS = {"thesis": "t", "momentum": "m", "catalysts": "c", "risk": "r", "opportunity_cost": "o"}
_CHECK = dict.fromkeys(["position_cap", "new_position_cap", "sector_cap", "trade_cap", "long_only", "cash"], True)
_RISK = {
    "sector": "Technology",
    "largest_position_after_pct": "25.0",
    "sector_weight_after_pct": "10.0",
    "main_risks": ["rates"],
    "invalidation": "thesis breaks",
    "confidence": "medium",
    "limits_check": _CHECK,
}


def sell_leg(label: str, ticker: str, fraction: str) -> dict[str, Any]:
    """A SELL leg of ``fraction`` of the position."""
    size = {"kind": "fraction_of_position", "fraction": fraction, "leg": None, "value": None}
    return {"leg": label, "action": "SELL", "ticker": ticker, "thesis": None, "size_intent": size}


def buy_proceeds_leg(label: str, ticker: str, source: str, fraction: str = "1.0000") -> dict[str, Any]:
    """A BUY funded by ``fraction`` of the proceeds of leg ``source``."""
    size = {"kind": "proceeds_of", "fraction": fraction, "leg": source, "value": None}
    return {"leg": label, "action": "BUY", "ticker": ticker, "thesis": "Because.", "size_intent": size}


def buy_value_leg(label: str, ticker: str, value: str) -> dict[str, Any]:
    """A BUY of ``value`` dollars from cash."""
    size = {"kind": "target_value", "fraction": None, "leg": None, "value": value}
    return {"leg": label, "action": "BUY", "ticker": ticker, "thesis": "Because.", "size_intent": size}


def idea(priority: int, *legs: dict[str, Any]) -> dict[str, Any]:
    """One trade idea."""
    return {
        "priority": priority,
        "rationale": f"rationale {priority}",
        "why_beats_hold": f"beats hold {priority}",
        "legs": list(legs),
        "risk_assessment": _RISK,
        "evidence_fact_ids": [],
    }


def output(*ideas: dict[str, Any], held: tuple[str, ...] = ("AAPL", "JPM")) -> ReasonerOutput:
    """A ``ReasonerOutput`` assessing every ticker in ``held`` as hold, plus ``ideas``."""
    holdings = [
        {"ticker": t, "stance": "hold", "thesis_status": "intact", "dims": _DIMS, "evidence_fact_ids": []} for t in held
    ]
    return ReasonerOutput.model_validate(
        {"summary": "s", "hold_alternative": "Hold everything.", "holdings": holdings, "ideas": list(ideas)}
    )


def hold_output() -> ReasonerOutput:
    """No ideas."""
    return output()


def pair_output() -> ReasonerOutput:
    """Sell a quarter of AAPL, buy MSFT with the proceeds."""
    return output(idea(1, sell_leg("sell1", "AAPL", "0.2500"), buy_proceeds_leg("buy1", "MSFT", "sell1")))
