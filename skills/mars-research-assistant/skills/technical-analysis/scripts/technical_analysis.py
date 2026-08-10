#!/usr/bin/env python3
"""Build deterministic technical-analysis artifacts from an offline OHLCV fixture."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from technical_chart import (  # noqa: E402
    chart_html,
    temporary_chart,
    visualization_result,
)


VISIBLE_BARS = 120
SMA_WINDOWS = (20, 50, 200)
MINIMUM_HISTORY_BARS = VISIBLE_BARS + max(SMA_WINDOWS) - 1
SWING_RADIUS = 2
ATR_WINDOW = 14
ATR_CLUSTER_RATIO = 0.5
SMA_DIRECTION_LOOKBACK = 5
ADJUSTED_METHODS = {
    "adjusted",
    "dividend-adjusted",
    "split-adjusted",
    "total-return-adjusted",
}


class TechnicalAnalysisError(ValueError):
    """Reject an invalid request or artifact operation."""


class DataQualityError(ValueError):
    """Describe a blocking data gap without producing technical evidence."""


@dataclass(frozen=True)
class Source:
    as_of: str
    status: str

    @property
    def label(self) -> str:
        return "yfinance EOD（非官方 best-effort）"


@dataclass(frozen=True)
class QualifiedHistory:
    timeframe: str
    timezone: str
    adjustment: str
    bars: list[dict[str, int | float | str]]
    stripped_incomplete_bar: bool


@dataclass(frozen=True)
class Swing:
    side: str
    price: float
    anchor_date: str
    confirmed_index: int


def _required_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TechnicalAnalysisError(f"{context} requires text")
    return value.strip()


def _data_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataQualityError(f"{context} requires text")
    return value.strip()


def _timestamp(value: object, context: str) -> datetime:
    text = _required_text(value, context)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise DataQualityError(f"{context} requires an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataQualityError(f"{context} requires a timezone-aware timestamp")
    return parsed


def _identity(value: object) -> dict[str, Any] | None:
    """Optional case identity carried from the fixture into the evidence
    payload so downstream artifacts can bind the evidence to a case."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TechnicalAnalysisError("identity requires an object")
    record: dict[str, Any] = {
        field: _required_text(value.get(field), f"identity {field}")
        for field in ("issuer_id", "listing_id", "case_id")
    }
    for field in ("artifact_version", "schema_version"):
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or number != 1:
            raise TechnicalAnalysisError(f"identity {field} must be 1")
        record[field] = number
    return record


def _source(value: object) -> Source:
    if not isinstance(value, dict):
        raise TechnicalAnalysisError("provider requires an object")
    if _required_text(value.get("name"), "provider") != "yfinance EOD":
        raise TechnicalAnalysisError("provider must be yfinance EOD")
    if _required_text(value.get("kind"), "provider") != "public_best_effort":
        raise TechnicalAnalysisError("provider must be yfinance public best-effort")
    status = _required_text(value.get("status", "available"), "provider")
    if status not in {"available", "rate_limited", "unavailable"}:
        raise TechnicalAnalysisError(
            "provider status must be available, rate_limited, or unavailable"
        )
    as_of = _required_text(value.get("as_of"), "provider")
    _timestamp(as_of, "provider as_of")
    return Source(as_of=as_of, status=status)


def _number(value: object, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataQualityError(f"OHLCV bar requires numeric {field}")
    if not math.isfinite(value):
        raise DataQualityError(f"OHLCV bar requires finite {field}")
    return value


def _normalize_bar(value: object) -> dict[str, int | float | str]:
    if not isinstance(value, dict):
        raise DataQualityError("OHLCV bar must be an object")
    timestamp = _data_text(value.get("timestamp"), "OHLCV bar")
    _timestamp(timestamp, "OHLCV bar")
    open_price = _number(value.get("open"), "open")
    high_price = _number(value.get("high"), "high")
    low_price = _number(value.get("low"), "low")
    close_price = _number(value.get("close"), "close")
    volume = _number(value.get("volume"), "volume")
    if volume <= 0:
        raise DataQualityError("OHLCV bar requires positive volume")
    if min(open_price, high_price, low_price, close_price) <= 0:
        raise DataQualityError("OHLCV bar requires positive prices")
    if not low_price <= min(open_price, close_price) <= max(
        open_price, close_price
    ) <= high_price:
        raise DataQualityError("OHLCV bar has inconsistent price bounds")
    return {
        "timestamp": timestamp,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
    }


def _qualified_history(value: object, requested_timeframe: str) -> QualifiedHistory:
    if not isinstance(value, dict):
        raise DataQualityError("OHLCV requires an object")
    timeframe = _data_text(value.get("timeframe"), "OHLCV")
    if timeframe != requested_timeframe:
        raise DataQualityError("OHLCV timeframe does not match requested timeframe")
    if timeframe != "1D":
        raise DataQualityError("technical analysis currently requires 1D OHLCV")
    if value.get("time_range_suitable") is not True:
        raise DataQualityError(
            "OHLCV time range is not suitable for the requested analysis"
        )
    _data_text(value.get("time_range"), "OHLCV")
    timezone = _data_text(value.get("timezone"), "OHLCV")
    try:
        declared_timezone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise DataQualityError("OHLCV timezone must be a valid IANA timezone") from error
    adjustment = _data_text(value.get("adjustment"), "OHLCV")
    if adjustment not in ADJUSTED_METHODS:
        raise DataQualityError("OHLCV adjustment must be adjusted")
    raw_bars = value.get("bars")
    if not isinstance(raw_bars, list):
        raise DataQualityError("OHLCV bars require a list")

    stripped_incomplete_bar = False
    if any(
        isinstance(bar, dict) and bar.get("complete") is False
        for bar in raw_bars[:-1]
    ):
        raise DataQualityError("only the latest OHLCV bar may be incomplete")
    if (
        raw_bars
        and isinstance(raw_bars[-1], dict)
        and raw_bars[-1].get("complete") is False
    ):
        raw_bars = raw_bars[:-1]
        stripped_incomplete_bar = True

    bars = [_normalize_bar(bar) for bar in raw_bars]
    timestamps = [str(bar["timestamp"]) for bar in bars]
    parsed_timestamps = [
        _timestamp(timestamp, "OHLCV bar") for timestamp in timestamps
    ]
    if any(
        timestamp.utcoffset()
        != timestamp.astimezone(declared_timezone).utcoffset()
        for timestamp in parsed_timestamps
    ):
        raise DataQualityError(
            "OHLCV bar timezone offset does not match declared timezone"
        )
    if any(
        previous >= current
        for previous, current in zip(parsed_timestamps, parsed_timestamps[1:])
    ):
        raise DataQualityError("OHLCV timestamps must be strictly increasing")

    coverage_start = _data_text(value.get("coverage_start"), "OHLCV")
    coverage_end = _data_text(value.get("coverage_end"), "OHLCV")
    try:
        coverage_start_date = date.fromisoformat(coverage_start)
        coverage_end_date = date.fromisoformat(coverage_end)
    except ValueError as error:
        raise DataQualityError("OHLCV coverage requires ISO 8601 dates") from error
    if coverage_start_date > coverage_end_date:
        raise DataQualityError("OHLCV coverage start must not follow coverage end")
    if bars and (
        timestamps[0][:10] != coverage_start
        or timestamps[-1][:10] != coverage_end
    ):
        raise DataQualityError("OHLCV coverage must exactly match actual bars")
    if len(bars) < MINIMUM_HISTORY_BARS:
        missing = MINIMUM_HISTORY_BARS - len(bars)
        raise DataQualityError(
            f"日线 OHLCV 缺少 {missing} 根已完成日线"
            f"（需要至少 {MINIMUM_HISTORY_BARS} 根，实际 {len(bars)} 根）。"
        )
    return QualifiedHistory(
        timeframe=timeframe,
        timezone=timezone,
        adjustment=adjustment,
        bars=bars,
        stripped_incomplete_bar=stripped_incomplete_bar,
    )


def _moving_average(values: list[float], window: int) -> list[float | None]:
    averages: list[float | None] = []
    rolling_total = 0.0
    for index, value in enumerate(values):
        rolling_total += value
        if index >= window:
            rolling_total -= values[index - window]
        average = rolling_total / window if index >= window - 1 else None
        averages.append(round(average, 6) if average is not None else None)
    return averages


def _true_ranges(bars: list[dict[str, int | float | str]]) -> list[float]:
    ranges: list[float] = []
    for index, bar in enumerate(bars):
        high = float(bar["high"])
        low = float(bar["low"])
        if index == 0:
            value = high - low
        else:
            previous_close = float(bars[index - 1]["close"])
            value = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        ranges.append(round(value, 6))
    return ranges


def _atr14(bars: list[dict[str, int | float | str]]) -> list[float | None]:
    return _moving_average(_true_ranges(bars), ATR_WINDOW)


def _confirmed_swings(
    bars: list[dict[str, int | float | str]],
) -> list[Swing]:
    swings: list[Swing] = []
    start = max(SWING_RADIUS, len(bars) - VISIBLE_BARS)
    stop = len(bars) - SWING_RADIUS
    for index in range(start, stop):
        low = float(bars[index]["low"])
        high = float(bars[index]["high"])
        neighbors = bars[index - SWING_RADIUS : index] + bars[
            index + 1 : index + SWING_RADIUS + 1
        ]
        anchor_date = str(bars[index]["timestamp"])[:10]
        confirmed_index = index + SWING_RADIUS
        if all(low < float(neighbor["low"]) for neighbor in neighbors):
            swings.append(
                Swing("support", low, anchor_date, confirmed_index)
            )
        if all(high > float(neighbor["high"]) for neighbor in neighbors):
            swings.append(
                Swing("resistance", high, anchor_date, confirmed_index)
            )
    return swings


def _cluster_levels(
    swings: list[Swing],
    side: str,
    latest_close: float,
    latest_atr: float,
) -> list[dict[str, Any]]:
    candidates = [
        swing
        for swing in swings
        if swing.side == side
        and (
            (side == "support" and swing.price <= latest_close)
            or (side == "resistance" and swing.price >= latest_close)
        )
    ]
    tolerance = round(latest_atr * ATR_CLUSTER_RATIO, 6)
    clusters: list[list[Swing]] = []
    for candidate in sorted(candidates, key=lambda item: (item.price, item.anchor_date)):
        matching = next(
            (
                cluster
                for cluster in clusters
                if abs(
                    candidate.price
                    - sum(item.price for item in cluster) / len(cluster)
                )
                <= tolerance
            ),
            None,
        )
        if matching is None:
            clusters.append([candidate])
        else:
            matching.append(candidate)

    levels: list[dict[str, Any]] = []
    for cluster in clusters:
        price = round(sum(item.price for item in cluster) / len(cluster), 6)
        levels.append(
            {
                "side": side,
                "method": "confirmed_swing_atr14_cluster",
                "lookback": VISIBLE_BARS,
                "anchor_dates": sorted(item.anchor_date for item in cluster),
                "touches": len(cluster),
                "price": price,
                "_confirmed_index": max(item.confirmed_index for item in cluster),
                "_distance": round(abs(price - latest_close), 6),
            }
        )
    levels.sort(
        key=lambda level: (
            -int(level["touches"]),
            -int(level["_confirmed_index"]),
            float(level["_distance"]),
            float(level["price"]),
        )
    )
    return levels[:2]


def _fallback_level(
    bars: list[dict[str, int | float | str]], side: str
) -> dict[str, Any]:
    visible = bars[-VISIBLE_BARS:]
    field = "low" if side == "support" else "high"
    selected = (
        min(visible, key=lambda bar: (float(bar[field]), str(bar["timestamp"])))
        if side == "support"
        else max(visible, key=lambda bar: (float(bar[field]), str(bar["timestamp"])))
    )
    return {
        "side": side,
        "method": "120d_extreme_fallback",
        "lookback": VISIBLE_BARS,
        "anchor_dates": [str(selected["timestamp"])[:10]],
        "touches": 1,
        "price": round(float(selected[field]), 6),
    }


def _key_levels(
    bars: list[dict[str, int | float | str]], latest_atr: float
) -> list[dict[str, Any]]:
    latest_close = float(bars[-1]["close"])
    swings = _confirmed_swings(bars)
    levels: list[dict[str, Any]] = []
    for side in ("support", "resistance"):
        ranked = _cluster_levels(swings, side, latest_close, latest_atr)
        if not ranked:
            ranked = [_fallback_level(bars, side)]
        for level in ranked:
            level.pop("_confirmed_index", None)
            level.pop("_distance", None)
        levels.extend(ranked)
    return levels


def _market_context(
    value: object, research_as_of: str, target_timezone: str
) -> dict[str, Any]:
    if value is None:
        return {"status": "not_provided"}
    if not isinstance(value, dict):
        return {"status": "invalid", "reason": "市场背景不是标准工件。"}
    try:
        status = _required_text(value.get("status", "available"), "market context")
    except TechnicalAnalysisError as error:
        return {"status": "invalid", "reason": str(error)}
    if status != "available":
        reason = value.get("reason")
        return {
            "status": "unavailable",
            "reason": (
                reason.strip()
                if isinstance(reason, str) and reason.strip()
                else f"市场背景状态：{status}"
            ),
        }
    try:
        as_of = _required_text(value.get("as_of"), "market context")
        source = _required_text(value.get("source"), "market context")
        timezone_name = _required_text(
            value.get("timezone"), "market context"
        )
        regime = _required_text(value.get("regime"), "market context")
        summary = _required_text(value.get("summary"), "market context")
        context_time = _timestamp(as_of, "market context as_of")
        research_time = _timestamp(research_as_of, "research as_of")
        target_zone = ZoneInfo(target_timezone)
    except (
        TechnicalAnalysisError,
        DataQualityError,
        ZoneInfoNotFoundError,
    ) as error:
        return {"status": "invalid", "reason": str(error)}
    if timezone_name != target_timezone:
        return {
            "status": "invalid",
            "reason": "市场背景时区与目标市场时区不一致。",
        }
    same_run = value.get("same_run") is True
    age_hours = round(
        (
            research_time.astimezone(target_zone)
            - context_time.astimezone(target_zone)
        ).total_seconds()
        / 3600,
        3,
    )
    if not same_run and (age_hours < 0 or age_hours > 24):
        return {
            "status": "stale",
            "as_of": as_of,
            "source": source,
            "timezone": timezone_name,
            "regime": regime,
            "age_hours": age_hours,
        }
    return {
        "status": "valid",
        "as_of": as_of,
        "source": source,
        "timezone": timezone_name,
        "regime": regime,
        "summary": summary,
        "same_run": same_run,
        "age_hours": max(age_hours, 0),
    }


def _technical_regime(latest: dict[str, float]) -> str:
    close = latest["close"]
    sma20 = latest["sma20"]
    sma50 = latest["sma50"]
    sma200 = latest["sma200"]
    if close > sma20 > sma50 > sma200:
        return "多头"
    if close < sma20 < sma50 < sma200:
        return "空头"
    return "震荡"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _source_attempt_metadata(
    fixture: dict[str, Any], default_attempts: int
) -> tuple[int, bool]:
    attempts = fixture.get("source_attempts", default_attempts)
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise TechnicalAnalysisError("source_attempts must be an integer")
    if attempts not in {1, 2}:
        raise TechnicalAnalysisError("source_attempts must be one or two")
    expanded = fixture.get("expanded_window_retry_used", attempts == 2)
    if not isinstance(expanded, bool):
        raise TechnicalAnalysisError(
            "expanded_window_retry_used must be boolean"
        )
    if expanded != (attempts == 2):
        raise TechnicalAnalysisError(
            "expanded_window_retry_used contradicts source_attempts"
        )
    return attempts, expanded


def _percentage_change(current: float, reference: float) -> float:
    if reference == 0:
        raise DataQualityError("derived percentage metric requires non-zero reference")
    return round((current / reference - 1) * 100, 6)


def _derived_metrics(
    bars: list[dict[str, int | float | str]],
    closes: list[float],
    volumes: list[float],
    moving_averages: dict[str, list[float | None]],
    latest: dict[str, float],
    key_levels: list[dict[str, Any]],
) -> dict[str, Any]:
    close = latest["close"]
    price_vs_sma_pct = {
        str(window): _percentage_change(close, latest[f"sma{window}"])
        for window in SMA_WINDOWS
    }
    sma_direction: dict[str, dict[str, int | float | str]] = {}
    for window in SMA_WINDOWS:
        values = moving_averages[str(window)]
        current = values[-1]
        previous = values[-1 - SMA_DIRECTION_LOOKBACK]
        if current is None or previous is None:
            raise DataQualityError(
                f"SMA{window} direction requires complete fixed-window values"
            )
        change_pct = _percentage_change(current, previous)
        direction = "rising" if change_pct > 0 else "falling"
        if change_pct == 0:
            direction = "flat"
        sma_direction[str(window)] = {
            "lookback_bars": SMA_DIRECTION_LOOKBACK,
            "change_pct": change_pct,
            "direction": direction,
        }

    returns = {
        str(window): _percentage_change(close, closes[-1 - window])
        for window in (20, 60, 120)
    }
    high_120 = max(float(bar["high"]) for bar in bars[-VISIBLE_BARS:])
    support = min(
        (
            level
            for level in key_levels
            if level["side"] == "support"
        ),
        key=lambda level: abs(float(level["price"]) - close),
    )
    resistance = min(
        (
            level
            for level in key_levels
            if level["side"] == "resistance"
        ),
        key=lambda level: abs(float(level["price"]) - close),
    )
    return {
        "price_vs_sma_pct": price_vs_sma_pct,
        "sma_direction": sma_direction,
        "completed_bar_returns_pct": returns,
        "atr14_pct_of_close": round(latest["atr14"] / close * 100, 6),
        "volume_vs_20d_average_ratio": round(
            latest["volume"] / latest["volume20_average"], 6
        ),
        "drawdown_from_120d_high_pct": round((high_120 - close) / high_120 * 100, 6),
        "distance_to_nearest_support_pct": round(
            (close - float(support["price"])) / close * 100, 6
        ),
        "distance_to_nearest_resistance_pct": round(
            (float(resistance["price"]) - close) / close * 100, 6
        ),
    }


def _build_evidence(
    fixture: dict[str, Any],
    source: Source,
    history: QualifiedHistory,
    retry_count: int,
    identity: dict[str, Any] | None,
) -> dict[str, Any]:
    bars = history.bars
    closes = [float(bar["close"]) for bar in bars]
    volumes = [float(bar["volume"]) for bar in bars]
    moving_averages = {
        str(window): _moving_average(closes, window) for window in SMA_WINDOWS
    }
    atr14 = _atr14(bars)
    latest_atr = atr14[-1]
    if latest_atr is None or latest_atr <= 0:
        raise DataQualityError("ATR14 requires positive price ranges")
    latest_sma20 = moving_averages["20"][-1]
    latest_sma50 = moving_averages["50"][-1]
    latest_sma200 = moving_averages["200"][-1]
    if latest_sma20 is None or latest_sma50 is None or latest_sma200 is None:
        raise DataQualityError("latest moving averages require complete values")
    latest: dict[str, float] = {
        "close": round(closes[-1], 6),
        "sma20": latest_sma20,
        "sma50": latest_sma50,
        "sma200": latest_sma200,
        "volume": round(volumes[-1], 6),
        "volume20_average": round(sum(volumes[-20:]) / 20, 6),
        "atr14": round(latest_atr, 6),
    }
    key_levels = _key_levels(bars, latest_atr)
    derived_metrics = _derived_metrics(
        bars,
        closes,
        volumes,
        moving_averages,
        latest,
        key_levels,
    )
    regime = _technical_regime(latest)
    source_attempts, expanded_window_retry_used = _source_attempt_metadata(
        fixture, retry_count + 1
    )
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "qualified",
        "symbol": _required_text(fixture.get("instrument"), "fixture"),
        "source": {
            "provider": "yfinance",
            "label": source.label,
            "classification": "non_official_best_effort",
            "as_of": source.as_of,
            "attempts": source_attempts,
            "expanded_window_retry_used": expanded_window_retry_used,
        },
        "timeframe": history.timeframe,
        "timezone": history.timezone,
        "as_of": source.as_of,
        "research_as_of": _required_text(
            fixture.get("research_as_of"), "fixture"
        ),
        "adjustment": history.adjustment,
        "bars_used": len(bars),
        "visible_bars": VISIBLE_BARS,
        "stripped_incomplete_latest_bar": history.stripped_incomplete_bar,
        "ohlcv": bars,
        "indicators": {
            "sma": moving_averages,
            "atr14": atr14,
            "latest": latest,
        },
        "derived_metrics": derived_metrics,
        "key_levels": key_levels,
        "regime": regime,
        "priority_scenario": {
            "name": {"多头": "bull", "震荡": "range", "空头": "bear"}[regime],
            "label": regime,
            "basis": "technical_regime",
        },
        "market_context": _market_context(
            fixture.get("market_context"),
            _required_text(fixture.get("research_as_of"), "fixture"),
            history.timezone,
        ),
    }
    if identity is not None:
        evidence["identity"] = identity
    technical_identity = {
        key: value for key, value in evidence.items() if key != "market_context"
    }
    digest = sha256(_canonical_json(technical_identity).encode("utf-8")).hexdigest()
    evidence["evidence_id"] = f"sha256:{digest}"
    return evidence


def _format_number(value: float | int) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _nearest_level(
    evidence: dict[str, Any], side: str
) -> dict[str, Any]:
    close = float(evidence["indicators"]["latest"]["close"])
    levels = [
        level for level in evidence["key_levels"] if level["side"] == side
    ]
    return min(levels, key=lambda level: abs(float(level["price"]) - close))


def _relative_sma_phrase(window: str, value: float) -> str:
    relation = "高" if value >= 0 else "低"
    return f"较 SMA{window} {relation} {_format_number(abs(value))}%"


def _context_relationship(technical_regime: str, context_regime: str) -> str:
    normalized = context_regime.strip().lower()
    matching = {
        "多头": {"多头", "bull", "bullish", "risk_on", "risk-on"},
        "震荡": {"震荡", "range", "neutral", "sideways"},
        "空头": {"空头", "bear", "bearish", "risk_off", "risk-off"},
    }
    return "共振" if normalized in matching[technical_regime] else "冲突"


def _analysis_markdown(evidence: dict[str, Any]) -> str:
    latest = evidence["indicators"]["latest"]
    metrics = evidence["derived_metrics"]
    support = _nearest_level(evidence, "support")
    resistance = _nearest_level(evidence, "resistance")
    context = evidence["market_context"]
    price_vs_sma = metrics["price_vs_sma_pct"]
    directions = metrics["sma_direction"]
    returns = metrics["completed_bar_returns_pct"]
    priority = evidence["priority_scenario"]["label"]
    direction_labels = {
        "rising": "上行",
        "falling": "下行",
        "flat": "走平",
    }
    nearest_support = _format_number(support["price"])
    nearest_resistance = _format_number(resistance["price"])
    regime = evidence["regime"]
    regime_basis = {
        "多头": "价格位于三组均线上方，且均线次序由短到长依次走高",
        "震荡": "价格与均线次序分化，尚未形成一致趋势",
        "空头": "价格位于三组均线下方，且均线次序由短到长依次走低",
    }[regime]
    bull_support = (
        "当前满足多头支持条件"
        if regime == "多头"
        else f"当前不满足多头支持条件（当前分类为{regime}）"
    )
    range_support = (
        "当前满足震荡支持条件"
        if regime == "震荡"
        else f"当前不满足震荡优先条件（当前分类为{regime}）"
    )
    bear_support = (
        "当前满足空头支持条件"
        if regime == "空头"
        else f"当前不满足空头支持条件（当前分类为{regime}）"
    )
    lines = [
        f"# 技术面分析：{evidence['symbol']}",
        "",
        f"证据标识：`{evidence['evidence_id']}`",
        f"研究截至：{evidence['research_as_of']}",
        "",
        "## 数据状态",
        "- 状态：合格。",
        "- 数据源：yfinance（非官方、best-effort，唯一官方内置来源）。",
        (
            f"- 来源：{evidence['source']['label']}（as_of："
            f"{evidence['source']['as_of']}）"
        ),
        (
            f"- 已完成日线：{evidence['bars_used']} 根；时区："
            f"{evidence['timezone']}；复权：{evidence['adjustment']}。"
        ),
        (
            "- 同源扩大窗口重试："
            + ("已使用一次。" if evidence["source"]["expanded_window_retry_used"] else "未使用。")
        ),
        "",
        "## 当前结论",
        (
            f"当前技术结构为**{evidence['regime']}**，当前优先情景："
            f"**{priority}**。{regime_basis}；最近阻力在 "
            f"{nearest_resistance}，距现价 "
            f"{_format_number(metrics['distance_to_nearest_resistance_pct'])}%，"
            "应以已完成日线是否突破或失守关键位作为下一步验证。"
        ),
        "",
        "## 趋势、位置与确认",
        (
            f"- **趋势**：最新收盘 {_format_number(latest['close'])}，"
            f"{_relative_sma_phrase('20', price_vs_sma['20'])}，"
            f"{_relative_sma_phrase('50', price_vs_sma['50'])}，"
            f"{_relative_sma_phrase('200', price_vs_sma['200'])}。"
        ),
        (
            f"- **均线方向**：固定回看 {SMA_DIRECTION_LOOKBACK} 根已完成日线，"
            f"SMA20/50/200 分别{direction_labels[directions['20']['direction']]}/"
            f"{direction_labels[directions['50']['direction']]}/"
            f"{direction_labels[directions['200']['direction']]}，"
            f"变化 {_format_number(directions['20']['change_pct'])}% / "
            f"{_format_number(directions['50']['change_pct'])}% / "
            f"{_format_number(directions['200']['change_pct'])}%。"
        ),
        (
            "- **动量**：20/60/120 根收益分别为 "
            f"{_format_number(returns['20'])}% / "
            f"{_format_number(returns['60'])}% / "
            f"{_format_number(returns['120'])}%；"
            f"距 120 日高点回撤 "
            f"{_format_number(metrics['drawdown_from_120d_high_pct'])}%。"
        ),
        (
            f"- **参与度**：最新量为 20 日均量的 "
            f"{_format_number(metrics['volume_vs_20d_average_ratio'])} 倍"
            f"（{_format_number(latest['volume'])} / "
            f"{_format_number(latest['volume20_average'])}）。"
        ),
        (
            f"- **波动**：ATR14 占收盘价 "
            f"{_format_number(metrics['atr14_pct_of_close'])}%；"
            f"距最近支撑 {nearest_support} 为 "
            f"{_format_number(metrics['distance_to_nearest_support_pct'])}%，"
            f"距最近阻力 {nearest_resistance} 为 "
            f"{_format_number(metrics['distance_to_nearest_resistance_pct'])}%。"
        ),
        "- 以上判断只基于已完成日线的技术面证据，不包含基本面或交易指令。",
        "",
        "## 当前优先情景",
        (
            f"- **{priority}**。优先级来自确定性的均线次序分类；"
            "它是待关键位验证的工作情景，不是胜率、评分或承诺。"
        ),
        "",
        "## 情景路径",
        "",
        "### 多头情景",
        (
            f"- 支持条件：{bull_support}；多头成立要求收盘位于 "
            "SMA20/50/200 上方，"
            f"20/60/120 根收益为 {_format_number(returns['20'])}% / "
            f"{_format_number(returns['60'])}% / "
            f"{_format_number(returns['120'])}%。"
        ),
        (
            f"- 有利表现：已完成日线收于阻力 {nearest_resistance} 上方，"
            "且成交量不低于其 20 日平均。"
        ),
        (
            f"- 不利表现：当前分类为{regime}；现价距离阻力 "
            f"{_format_number(metrics['distance_to_nearest_resistance_pct'])}%，"
            f"ATR14 占收盘价 {_format_number(metrics['atr14_pct_of_close'])}%，"
            "突破前仍有阻力与波动约束。"
        ),
        (
            f"- 触发条件：已完成日线突破 {nearest_resistance}，"
            f"并保持在 SMA20 {_format_number(latest['sma20'])} 上方。"
        ),
        (
            f"- 失效条件：已完成日线跌破支撑 {nearest_support}。"
        ),
        "",
        "### 震荡情景",
        (
            f"- 支持条件：{range_support}；价格仍处于支撑 "
            f"{nearest_support} 与阻力 "
            f"{nearest_resistance} 的可审计区间内。"
        ),
        (
            f"- 有利表现：已完成日线继续守住 {nearest_support}，"
            f"但未能收于 {nearest_resistance} 上方。"
        ),
        (
            f"- 不利表现：当前分类为{regime}；三组均线方向为"
            f"{direction_labels[directions['20']['direction']]}/"
            f"{direction_labels[directions['50']['direction']]}/"
            f"{direction_labels[directions['200']['direction']]}，"
            "任一侧突破会削弱震荡解释。"
        ),
        (
            f"- 触发条件：已完成日线持续收在 {nearest_support} 至 "
            f"{nearest_resistance} 之间。"
        ),
        (
            f"- 失效条件：已完成日线收于 {nearest_resistance} 上方"
            f"或 {nearest_support} 下方。"
        ),
        "",
        "### 空头情景",
        (
            f"- 支持条件：{bear_support}；距 120 日高点已有 "
            f"{_format_number(metrics['drawdown_from_120d_high_pct'])}% 回撤，"
            "下破关键位时可作为风险验证。"
        ),
        (
            f"- 有利表现：已完成日线跌破支撑 {nearest_support}，"
            f"并收于 SMA20 {_format_number(latest['sma20'])} 下方。"
        ),
        (
            f"- 不利表现：当前分类为{regime}；"
            f"{_relative_sma_phrase('20', price_vs_sma['20'])}、"
            f"{_relative_sma_phrase('50', price_vs_sma['50'])}、"
            f"{_relative_sma_phrase('200', price_vs_sma['200'])}；"
            f"若收于阻力 {nearest_resistance} 上方，将削弱空头解释。"
        ),
        (
            f"- 触发条件：已完成日线跌破 {nearest_support}。"
        ),
        (
            f"- 失效条件：已完成日线收于阻力 {nearest_resistance} 上方。"
        ),
        "",
        "## 关键位",
    ]
    for level in evidence["key_levels"]:
        side_label = "支撑" if level["side"] == "support" else "阻力"
        lines.append(
            f"- **{side_label} {_format_number(level['price'])}**："
            f"{level['touches']} 次确认，锚点日期 "
            f"{'、'.join(level['anchor_dates'])}。"
        )
    lines.extend(
        [
            "",
            "## 市场背景",
        ]
    )
    if context["status"] == "valid":
        relationship = _context_relationship(regime, context["regime"])
        lines.append(
            f"- 已纳入背景：{context['summary']}（regime：{context['regime']}；"
            f"来源：{context['source']}；as_of：{context['as_of']}）。"
            f"该背景与当前{priority}优先情景形成{relationship}；"
            "背景只用于解释共振或冲突，"
            "不改变图表、指标或关键位。"
        )
    else:
        reason = {
            "not_provided": "未提供市场背景",
            "stale": "市场背景已超过 24 小时有效期",
            "unavailable": context.get("reason", "市场背景不可用"),
            "invalid": context.get("reason", "市场背景无效"),
        }.get(context["status"], "市场背景不可用")
        lines.append(f"- {reason}；本报告仅基于技术面证据。")
    lines.extend(
        [
            "",
            "## 审计明细",
        ]
    )
    for level in evidence["key_levels"]:
        lines.append(
            f"- {_format_number(level['price'])}："
            f"method={level['method']}；lookback={level['lookback']}；"
            f"anchor_dates={','.join(level['anchor_dates'])}；"
            f"touches={level['touches']}。"
        )
    lines.extend(
        [
            "",
            "## 数据限制",
            "- yfinance 为非官方、best-effort 数据源；本工件不构成实时行情或交易建议。",
        ]
    )
    if evidence["stripped_incomplete_latest_bar"]:
        lines.append("- 已安全剔除一根未完成的最新日线。")
    return "\n".join(lines).rstrip() + "\n"


def _failure_markdown(
    symbol: str,
    research_as_of: str,
    source: Source,
    reason: str,
    attempts: int,
) -> str:
    return "\n".join(
        [
            f"# 技术面分析：{symbol}",
            "",
            f"研究截至：{research_as_of}",
            "",
            "## 数据状态",
            "- 状态：不合格，技术结论整体关闭。",
            "- 数据源：yfinance（非官方、best-effort，唯一官方内置来源）。",
            f"- 来源：{source.label}（as_of：{source.as_of}）",
            f"- 同源取数尝试：{attempts} 次（最多一次扩大历史窗口重试）。",
            "",
            "## 数据缺口",
            f"- {reason}",
        ]
    ) + "\n"


def _verify_artifacts(files: dict[str, str], evidence_id: str | None) -> None:
    if evidence_id is None:
        if set(files) != {"analysis.md"}:
            raise TechnicalAnalysisError("failed analysis may only contain analysis.md")
        return
    if set(files) != {"analysis.md", "evidence.json"}:
        raise TechnicalAnalysisError(
            "qualified analysis requires analysis.md and evidence.json"
        )
    parsed = json.loads(files["evidence.json"])
    if parsed.get("evidence_id") != evidence_id:
        raise TechnicalAnalysisError("evidence JSON identity mismatch")
    if evidence_id not in files["analysis.md"]:
        raise TechnicalAnalysisError("artifact evidence identities do not match")


def _validate_output_dir(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    runtime_root = Path(__file__).resolve().parents[3]
    if runtime_root == resolved or runtime_root in resolved.parents:
        raise TechnicalAnalysisError("output_dir must not be inside the Skill runtime package")


def _atomic_write(output_dir: Path, files: dict[str, str]) -> None:
    if output_dir.exists():
        raise TechnicalAnalysisError("output_dir must not already exist")
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=parent)
    )
    try:
        for name, content in files.items():
            (temporary / name).write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _write_failure_package(
    output_dir: Path,
    symbol: str,
    research_as_of: str,
    source: Source,
    reason: str,
    attempts: int,
) -> None:
    files = {
        "analysis.md": _failure_markdown(
            symbol,
            research_as_of,
            source,
            reason,
            attempts,
        )
    }
    _verify_artifacts(files, None)
    _atomic_write(output_dir, files)


def render_fixture(
    fixture: dict[str, Any],
    output_dir: Path,
    *,
    open_chart: bool = True,
) -> dict[str, Any]:
    _validate_output_dir(output_dir)
    symbol = _required_text(fixture.get("instrument"), "fixture")
    timeframe = _required_text(fixture.get("timeframe"), "fixture")
    research_as_of = _required_text(fixture.get("research_as_of"), "fixture")
    research_moment = _timestamp(research_as_of, "research as_of")
    identity = _identity(fixture.get("identity"))
    listing_id = identity.get("listing_id") if identity is not None else None
    bare_us_alias = (
        isinstance(listing_id, str)
        and "." not in symbol
        and listing_id == f"{symbol}.US"
    )
    if identity is not None and listing_id != symbol and not bare_us_alias:
        raise TechnicalAnalysisError(
            "identity listing_id must match fixture instrument"
        )
    source = _source(fixture.get("provider"))
    if _timestamp(source.as_of, "provider as_of") > research_moment:
        raise TechnicalAnalysisError(
            "provider as_of must not be after research_as_of"
        )
    if source.status != "available":
        source_attempts, _ = _source_attempt_metadata(fixture, 1)
        source_error = fixture.get("source_error")
        reason = f"yfinance 暂不可用：{source.status}"
        if isinstance(source_error, str) and source_error.strip():
            reason += f"（{source_error.strip()}）"
        reason += "。"
        _write_failure_package(
            output_dir,
            symbol,
            research_as_of,
            source,
            reason,
            source_attempts,
        )
        return {
            "artifacts": ["analysis.md"],
            "visualization": visualization_result(None, open_chart=open_chart),
        }

    raw_attempts = fixture.get("attempts")
    if raw_attempts is None:
        attempts = [fixture.get("ohlcv")]
    elif isinstance(raw_attempts, list) and 1 <= len(raw_attempts) <= 2:
        attempts = raw_attempts
    else:
        raise TechnicalAnalysisError("attempts must contain one or two OHLCV payloads")
    history: QualifiedHistory | None = None
    last_error: DataQualityError | None = None
    retry_count = 0
    for index, payload in enumerate(attempts):
        try:
            history = _qualified_history(payload, timeframe)
            retry_count = index
            break
        except DataQualityError as error:
            last_error = error
    if history is None:
        reason = str(last_error or DataQualityError("OHLCV unavailable"))
        source_error = fixture.get("source_error")
        if isinstance(source_error, str) and source_error.strip():
            reason += f"；同源请求错误：{source_error.strip()}"
        source_attempts, _ = _source_attempt_metadata(
            fixture, len(attempts)
        )
        _write_failure_package(
            output_dir,
            symbol,
            research_as_of,
            source,
            reason,
            source_attempts,
        )
        return {
            "artifacts": ["analysis.md"],
            "visualization": visualization_result(None, open_chart=open_chart),
        }

    try:
        evidence = _build_evidence(fixture, source, history, retry_count, identity)
    except DataQualityError as error:
        source_attempts, _ = _source_attempt_metadata(
            fixture, retry_count + 1
        )
        _write_failure_package(
            output_dir,
            symbol,
            research_as_of,
            source,
            str(error),
            source_attempts,
        )
        return {
            "artifacts": ["analysis.md"],
            "visualization": visualization_result(None, open_chart=open_chart),
        }
    evidence_json = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    files = {
        "analysis.md": _analysis_markdown(evidence),
        "evidence.json": evidence_json,
    }
    _verify_artifacts(files, evidence["evidence_id"])
    chart_path = temporary_chart(chart_html(evidence))
    try:
        _atomic_write(output_dir, files)
    except Exception:
        shutil.rmtree(chart_path.parent, ignore_errors=True)
        raise
    return {
        "artifacts": ["analysis.md", "evidence.json"],
        "visualization": visualization_result(
            chart_path,
            open_chart=open_chart,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Generate the temporary chart without requesting a browser open.",
    )
    arguments = parser.parse_args()
    try:
        fixture = json.loads(arguments.input.read_text(encoding="utf-8"))
        if not isinstance(fixture, dict):
            raise TechnicalAnalysisError("fixture must be a JSON object")
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
