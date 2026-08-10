#!/usr/bin/env python3
"""Fetch one yfinance history snapshot and build a technical-analysis package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable


sys.dont_write_bytecode = True

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from technical_analysis import (  # noqa: E402
    DataQualityError,
    TechnicalAnalysisError,
    _qualified_history,
    render_fixture,
)


DOWNLOAD_PERIODS = ("18mo", "3y")


def _frame_payload(frame: Any, now: datetime) -> dict[str, Any]:
    bars: list[dict[str, Any]] = []
    for timestamp, row in frame.iterrows():
        timestamp_text = timestamp.isoformat()
        bar = {
            "timestamp": timestamp_text,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row["Volume"]),
        }
        bars.append(bar)
    if bars:
        last_timestamp = datetime.fromisoformat(str(bars[-1]["timestamp"]))
        frame_timezone = getattr(
            getattr(frame, "index", None), "tz", None
        ) or getattr(frame, "timezone", None)
        timezone_name = str(frame_timezone or last_timestamp.tzinfo or "")
        coverage_start = str(bars[0]["timestamp"])[:10]
        completed = [
            bar for bar in bars if bar.get("complete", True) is not False
        ]
        coverage_end = (
            str(completed[-1]["timestamp"])[:10]
            if completed
            else coverage_start
        )
    else:
        timezone_name = ""
        coverage_start = now.date().isoformat()
        coverage_end = coverage_start
    return {
        "timeframe": "1D",
        "time_range_suitable": True,
        "time_range": f"{coverage_start} 至 {coverage_end}",
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "timezone": timezone_name,
        "adjustment": "adjusted",
        "bars": bars,
    }


def build_yfinance_fixture(
    symbol: str,
    *,
    ticker_factory: Callable[[str], Any],
    now: datetime,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Download at most twice from yfinance, widening only the history period."""
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise TechnicalAnalysisError("symbol requires text")
    if now.tzinfo is None or now.utcoffset() is None:
        raise TechnicalAnalysisError("now requires a timezone-aware timestamp")
    ticker = ticker_factory(normalized_symbol)
    attempts: list[dict[str, Any]] = []
    download_error: Exception | None = None
    request_count = 0
    qualified = False
    for period in DOWNLOAD_PERIODS:
        request_count += 1
        try:
            frame = ticker.history(
                period=period,
                interval="1d",
                end=now.astimezone(timezone.utc).date().isoformat(),
                auto_adjust=True,
                actions=False,
                repair=False,
                raise_errors=False,
            )
            payload = _frame_payload(frame, now)
            attempts.append(payload)
            _qualified_history(payload, "1D")
            qualified = True
            break
        except (DataQualityError, TechnicalAnalysisError) as error:
            download_error = error
            continue
        except Exception as error:  # yfinance/network boundary
            download_error = error
            continue

    as_of = now.isoformat()
    fixture: dict[str, Any] = {
        "instrument": normalized_symbol,
        "timeframe": "1D",
        "research_as_of": as_of,
        "provider": {
            "name": "yfinance EOD",
            "kind": "public_best_effort",
            "as_of": as_of,
            "status": "available" if attempts else "unavailable",
        },
        "source_attempts": request_count,
        "expanded_window_retry_used": request_count == 2,
    }
    if attempts:
        fixture["attempts"] = attempts
    if not qualified and download_error is not None:
        fixture["source_error"] = type(download_error).__name__
    if identity is not None:
        fixture["identity"] = identity
    return fixture


def _identity_from_arguments(arguments: argparse.Namespace) -> dict[str, Any] | None:
    values = (arguments.issuer_id, arguments.listing_id, arguments.case_id)
    if any(value is not None for value in values) and not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise TechnicalAnalysisError(
            "--issuer-id, --listing-id, and --case-id must be provided together"
        )
    if not all(value is not None for value in values):
        return None
    return {
        "issuer_id": arguments.issuer_id.strip(),
        "listing_id": arguments.listing_id.strip().upper(),
        "case_id": arguments.case_id.strip(),
        "artifact_version": 1,
        "schema_version": 1,
    }


def load_market_context(path: Path) -> dict[str, Any]:
    """Convert unreadable optional context into a non-blocking status."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return {
            "status": "invalid",
            "reason": type(error).__name__,
        }
    except OSError as error:
        return {
            "status": "unavailable",
            "reason": type(error).__name__,
        }
    if not isinstance(parsed, dict):
        return {
            "status": "invalid",
            "reason": "market context must be a JSON object",
        }
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot yfinance technical analysis. It performs no live, scheduled, "
            "or background updates."
        )
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--issuer-id", help="case issuer_id for evidence binding")
    parser.add_argument("--listing-id", help="case listing_id; must match --symbol")
    parser.add_argument("--case-id", help="case_id for evidence binding")
    parser.add_argument("--market-context", type=Path)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Generate the temporary chart without requesting a browser open.",
    )
    arguments = parser.parse_args()
    try:
        import yfinance  # type: ignore[import-not-found]

        now = datetime.now(timezone.utc)
        fixture = build_yfinance_fixture(
            arguments.symbol,
            ticker_factory=yfinance.Ticker,
            now=now,
            identity=_identity_from_arguments(arguments),
        )
        if arguments.market_context is not None:
            fixture["market_context"] = load_market_context(
                arguments.market_context
            )
        delivery = render_fixture(
            fixture,
            arguments.output_dir,
            open_chart=not arguments.no_open,
        )
        print(json.dumps(delivery, ensure_ascii=False, sort_keys=True))
    except (
        OSError,
        json.JSONDecodeError,
        TechnicalAnalysisError,
        DataQualityError,
    ) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
