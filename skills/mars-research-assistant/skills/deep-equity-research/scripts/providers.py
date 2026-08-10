#!/usr/bin/env python3
"""Explicit data-provider adapter registry for Mars Research Assistant v1.0.3.

Per the v1.0.3 implementation contract section 8:

- ``ADAPTERS`` is a closed registry. ``yfinance`` covers us/hk/a_share quotes
  and K-line data with classification ``non_official_best_effort``;
  ``offline_fixture`` is the deterministic test adapter.
- ``resolve_provider(market_scope, requested=None)`` resolves explicitly and
  returns a full provider record ``{"provider", "classification",
  "market_scope", "resolved_as_of", "ledger_note"}`` suitable for the source
  ledger; every resolution generates one ledger record.
- A provider that does not support the requested market raises
  ``UnsupportedProviderError`` (fail-closed). There is **no silent fallback**:
  when ``requested`` differs from the default, ``requested`` wins and the
  switch is recorded in ``switch_note``.
- Financial statements, announcements and governance data never come from a
  quote provider: the source hierarchy still requires issuer / exchange /
  regulator primary sources.

Standard library only. CLI: ``--market-scope hk [--provider yfinance]``
prints the resolution record as JSON to stdout.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import sys
from typing import Any


class UnsupportedProviderError(ValueError):
    """Fail closed when no registered provider supports the market scope."""


SUPPORTED_MARKET_SCOPES = ("us", "hk", "a_share")

ADAPTERS: dict[str, dict[str, Any]] = {
    "yfinance": {
        "classification": "non_official_best_effort",
        "market_scopes": ("us", "hk", "a_share"),
        "provides": ["quote", "kline"],
        "note": "非官方行情源，尽力而为；财报/公告/治理不走该 provider。",
    },
    "offline_fixture": {
        "classification": "offline_fixture",
        "market_scopes": ("us", "hk", "a_share"),
        "provides": ["quote", "kline"],
        "note": "离线 fixture 适配器，仅供测试与验收复算。",
    },
}

DEFAULT_PROVIDER = "yfinance"

PRIMARY_SOURCE_NOTE = (
    "财报/公告/治理数据不走行情 provider；来源层级仍要求发行人/交易所/监管原始来源。"
)


def resolve_provider(market_scope: str, requested: str | None = None) -> dict[str, Any]:
    """Resolve the data provider for a market scope, explicitly and audibly.

    Raises ``UnsupportedProviderError`` when the market scope is not data
    addressable (e.g. ``ah_compare``), the requested provider is unknown, or
    the provider does not support the scope. Never falls back silently.
    """
    if market_scope not in SUPPORTED_MARKET_SCOPES:
        raise UnsupportedProviderError(
            f"no provider supports market scope '{market_scope}'; "
            f"supported: {list(SUPPORTED_MARKET_SCOPES)}"
        )
    name = requested if requested is not None else DEFAULT_PROVIDER
    adapter = ADAPTERS.get(name)
    if adapter is None:
        raise UnsupportedProviderError(
            f"unknown provider '{name}'; registered: {sorted(ADAPTERS)}"
        )
    if market_scope not in adapter["market_scopes"]:
        raise UnsupportedProviderError(
            f"provider '{name}' does not support market scope '{market_scope}'; "
            f"its scopes: {list(adapter['market_scopes'])}"
        )

    explicit = requested is not None
    record: dict[str, Any] = {
        "provider": name,
        "classification": adapter["classification"],
        "market_scope": market_scope,
        "resolved_as_of": datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
        "ledger_note": (
            f"provider resolution: market_scope={market_scope} provider={name} "
            f"classification={adapter['classification']} "
            f"selection={'explicit' if explicit else 'default_explicit'}; "
            f"{PRIMARY_SOURCE_NOTE}"
        ),
    }
    if explicit and requested != DEFAULT_PROVIDER:
        record["switch_note"] = (
            f"requested provider '{requested}' differs from default "
            f"'{DEFAULT_PROVIDER}'; requested wins per caller decision, "
            "no silent fallback."
        )
    return record


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式解析市场数据 provider 并输出来源账本记录（JSON）。"
    )
    parser.add_argument("--market-scope", required=True, help="us / hk / a_share。")
    parser.add_argument(
        "--provider",
        default=None,
        help="显式指定的 provider；缺省解析为默认 provider（仍为显式记录）。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        record = resolve_provider(args.market_scope, requested=args.provider)
    except UnsupportedProviderError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
