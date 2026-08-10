#!/usr/bin/env python3
"""Reproducible valuation engine for deep equity research (v1.0.3 Batch 3).

Runs the contract-frozen model set — probability-weighted three-scenario DCF,
reverse DCF, PVGO decomposition, PE/earnings multiple, EPV, EVA/residual
income, SOTP and a seeded Monte Carlo — from an explicit JSON input and writes
a recomputable JSON artifact. Missing inputs fail closed per model; no
fair-value number is ever invented.

The legacy three-scenario DCF (``results.dcf``) discounts hand-supplied
``scenario.free_cash_flows`` and is kept as an auditable baseline
(``model_role: "baseline"``); its ``scenario.margins`` only feed the terminal
checks.  An optional generic driver-based DCF (``models.dcf.driver_model`` →
``results.driver_dcf``) derives each scenario's FCF path from operating
drivers — NOPAT = revenue × operating_margin × (1 − tax_rate)，FCF = NOPAT +
D&A − capex − ΔNWC — so high-growth / transition / stable stages are explicit
year-by-year paths.  A generic quality gate (usable / conditional /
unreliable) decides whether the driver DCF may anchor a fundamental target;
the baseline never qualifies on its own, and no parameter may be reverse-
tuned to fit the market price.

Input provenance: every ``{"value": ...}`` input may carry ``source``
(``{"name", "kind", "as_of", "url"}``, kind from the contract enum; a
source object missing any of these fields is rejected fail closed),
``currency``, ``derivation`` and ``accounting_period``. Each computed model
result records them under ``inputs_provenance``; only fields actually present
in the input are recorded. A scenario-level source covers scenario fields
without their own source, and a segment-level source covers SOTP segment
inputs without their own source; inherited sources are marked with
``source_inherited_from``. reverse_dcf/monte_carlo reuse dcf inputs whose
provenance lives in the dcf result. Key inputs (the minimum set is dcf's
price/shares_outstanding/net_debt/wacc/terminal_growth plus every scenario
probability) that still lack a source after inheritance never block the
calculation: the model computes, lists them in its ``source_gaps`` and the
top-level ``data_gaps`` instead of staying silent.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from math import floor, isfinite
from pathlib import Path
import random
import re
from typing import Any


class ValuationError(ValueError):
    """Fail closed on malformed inputs instead of inventing valuation numbers."""


ENGINE = "skills/deep-equity-research/scripts/dcf.py"
ENGINE_VERSION = "1.0.0"
MODEL_VERSION = "v1.0.3-valuation-1"
PROBABILITY_TOLERANCE = 1e-6
REVERSE_DCF_GROWTH_BOUNDS = (-0.95, 3.0)
REVERSE_DCF_ITERATIONS = 120
SCENARIO_NAMES = ("bear", "base", "bull")
MODEL_ORDER = (
    "dcf",
    "reverse_dcf",
    "pvgo",
    "pe",
    "epv",
    "eva",
    "sotp",
    "monte_carlo",
)
RUNTIME_ROOT = Path(__file__).resolve().parents[3]
TRADE_DIRECTIVE = re.compile(
    r"买入|卖出|增持|减持|加仓|减仓|建仓|平仓|下单|持仓比例|做空|沽空|卖空|"
    r"\bbuy\b|\bsell\b|\bshort\b|\bposition size\b|\bplace (?:an )?order\b",
    re.IGNORECASE,
)
MARKET_SCOPES = {"us", "hk", "a_share", "ah_compare"}
CURRENCIES = {"USD", "HKD", "CNY"}

DCF_REQUIRED = (
    "price",
    "shares_outstanding",
    "net_debt",
    "wacc",
    "terminal_growth",
    "long_run_growth_cap",
    "mature_margin_benchmark",
)
PVGO_REQUIRED = (
    "normalized_free_cash_flow",
    "wacc",
    "net_debt",
    "shares_outstanding",
    "price",
)
EPV_REQUIRED = (
    "normalized_ebit",
    "tax_rate",
    "maintenance_capex",
    "wacc",
    "net_debt",
    "shares_outstanding",
    "price",
)
EVA_REQUIRED = (
    "invested_capital_start",
    "wacc",
    "terminal_growth",
    "net_debt",
    "shares_outstanding",
)
SOTP_REQUIRED = ("net_debt", "shares_outstanding", "holding_discount")
PE_EARNINGS_BASES = {"trailing_eps", "forward_eps", "normalized_eps"}
SOURCE_KINDS = {
    "sec_filing",
    "regulatory_filing",
    "issuer_ir",
    "exchange",
    "issuer_announcement",
    "credible_media",
    "public_quote",
    "valuation_assumption",
}
DCF_KEY_SOURCE_REQUIRED = (
    "price",
    "shares_outstanding",
    "net_debt",
    "wacc",
    "terminal_growth",
)
# Generic driver-based DCF (models.dcf.driver_model): FCF paths are derived
# from operating drivers instead of being hand-fed.  High-growth / transition
# / stable phases are expressed as explicit year-by-year driver paths (margin
# and growth fade); no parameter is ever tuned to fit the market price.
DRIVER_MODEL_KIND = "driver_dcf"
DRIVER_FORMULA = (
    "NOPAT = revenue × operating_margin × (1 − tax_rate)；"
    "FCF = NOPAT + D&A − capex − ΔNWC；高增长/过渡/稳定阶段由逐年显式驱动路径表达。"
)
DRIVER_ARRAY_FIELDS = (
    "revenue",
    "operating_margin",
    "tax_rate",
    "depreciation_amortization",
    "capex",
    "change_in_nwc",
)
DRIVER_SHARED_REQUIRED = (
    "shares_outstanding",
    "net_debt",
    "wacc",
    "terminal_growth",
    "long_run_growth_cap",
    "mature_margin_benchmark",
)
MIN_USABLE_FORECAST_PERIODS = 5
MAX_FORECAST_PERIODS = 30
STALE_SOURCE_DAYS = 400


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValuationError(f"{context} requires text")
    return value.strip()


def _is_version_one(value: object) -> bool:
    return type(value) is int and value == 1


def _guarded_text(value: object, context: str) -> str:
    text = _text(value, context)
    if TRADE_DIRECTIVE.search(text):
        raise ValuationError(f"{context} contains a trade directive")
    return text


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValuationError(f"{context} requires a finite number")
    result = float(value)
    if not isfinite(result):
        raise ValuationError(f"{context} requires a finite number")
    return result


def _as_of(value: object, context: str) -> str:
    text = _text(value, context)
    if "T" not in text:
        raise ValuationError(f"{context} requires a complete timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as error:
        raise ValuationError(f"{context} requires an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValuationError(f"{context} timestamp requires a timezone")
    return text


def _as_of_moment(value: object, context: str) -> datetime:
    text = _as_of(value, context)
    parsed = datetime.fromisoformat(
        text[:-1] + "+00:00" if text.endswith("Z") else text
    )
    return parsed.astimezone(timezone.utc)


def _check_source_times(node: object, computed: datetime, context: str) -> None:
    """Fail closed when any recorded source postdates the fixture computed_as_of."""
    if isinstance(node, dict):
        if {"name", "kind", "as_of", "url"}.issubset(node):
            moment = _as_of_moment(node["as_of"], f"{context} source")
            if moment > computed:
                raise ValuationError(
                    f"{context} source as_of is after fixture computed_as_of: "
                    f"{node['as_of']}"
                )
        for key, value in node.items():
            _check_source_times(value, computed, f"{context}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_source_times(value, computed, f"{context}[{index}]")


def _check_input_currencies(
    node: object, expected: str, context: str = "models"
) -> None:
    """Reject declared input currencies that lack an explicit FX conversion."""
    if isinstance(node, dict):
        if node.get("currency") is not None:
            declared = _text(node.get("currency"), f"{context} currency")
            if declared != expected:
                raise ValuationError(
                    f"{context} currency {declared} does not match fixture currency "
                    f"{expected}; no FX conversion is available"
                )
        for key, value in node.items():
            _check_input_currencies(value, expected, f"{context}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_input_currencies(value, expected, f"{context}[{index}]")


def _round6(value: float) -> float:
    return round(value, 6)


def _entry(entry: object, context: str) -> float | None:
    """Read a scalar ``{"value": ...}`` input; None marks a missing input."""
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ValuationError(f"{context} requires an input object")
    if entry.get("value") is None:
        return None
    return _number(entry["value"], context)


def _source(value: object, context: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValuationError(f"{context} requires a source object")
    name = _text(value.get("name"), f"{context} source")
    kind = _text(value.get("kind"), f"{context} source")
    if kind not in SOURCE_KINDS:
        raise ValuationError(f"{context} source kind is not allowed: {kind}")
    return {
        "name": name,
        "kind": kind,
        "as_of": _as_of(value.get("as_of"), f"{context} source"),
        "url": _text(value.get("url"), f"{context} source url"),
    }


def _list_provenance(
    values: list[float],
    source: dict[str, str] | None = None,
    inherited_from: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "value": values,
        "source": source,
        "currency": None,
        "derivation": None,
        "accounting_period": None,
    }
    if source is not None and inherited_from is not None:
        record["source_inherited_from"] = inherited_from
    return record


def _provenance(
    entry: object,
    context: str,
    inherited_from: str | None = None,
    fallback_source: dict[str, str] | None = None,
) -> tuple[float | None, dict[str, Any] | None]:
    """Read a scalar input and keep its provenance; None marks a missing input."""
    if entry is None:
        return None, None
    if not isinstance(entry, dict):
        raise ValuationError(f"{context} requires an input object")
    if entry.get("value") is None:
        return None, None
    value = _number(entry["value"], context)
    source: dict[str, str] | None = None
    inherited = None
    if entry.get("source") is not None:
        source = _source(entry["source"], context)
    elif fallback_source is not None:
        source = fallback_source
        inherited = inherited_from
    record: dict[str, Any] = {
        "value": value,
        "source": source,
        "currency": (
            _text(entry["currency"], f"{context} currency")
            if entry.get("currency") is not None
            else None
        ),
        "derivation": (
            _guarded_text(entry["derivation"], f"{context} derivation")
            if entry.get("derivation") is not None
            else None
        ),
        "accounting_period": (
            _text(entry["accounting_period"], f"{context} accounting_period")
            if entry.get("accounting_period") is not None
            else None
        ),
    }
    if inherited is not None:
        record["source_inherited_from"] = inherited
    return value, record


def _inputs_provenance(
    spec: dict[str, Any], names: tuple[str, ...], model: str
) -> tuple[dict[str, float | None], dict[str, Any]]:
    fields: dict[str, float | None] = {}
    provenance: dict[str, Any] = {}
    for name in names:
        value, record = _provenance(spec.get(name), f"{model} {name}")
        fields[name] = value
        if record is not None:
            provenance[name] = record
    return fields, provenance


def _missing(model: str, missing: list[str]) -> tuple[dict[str, Any], list[str]]:
    return (
        {"status": "missing_inputs", "missing": missing},
        [f"{model}: 缺少必要输入：{', '.join(missing)}，未输出公允价值数字。"],
    )


def _invalid(model: str, detail: str) -> tuple[dict[str, Any], list[str]]:
    return {"status": "invalid_inputs", "detail": detail}, [f"{model}: {detail}"]


def _dcf_equity(
    cash_flows: list[float], wacc: float, terminal_growth: float, net_debt: float
) -> tuple[float, float]:
    discounted = sum(
        cash_flow / (1 + wacc) ** year
        for year, cash_flow in enumerate(cash_flows, 1)
    )
    terminal = cash_flows[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    enterprise = discounted + terminal / (1 + wacc) ** len(cash_flows)
    return enterprise, enterprise - net_debt


def _reverse_enterprise_value(
    current_fcf: float,
    horizon: int,
    growth_rate: float,
    wacc: float,
    terminal_growth: float,
) -> float:
    cash_flows = [
        current_fcf * (1 + growth_rate) ** year for year in range(1, horizon + 1)
    ]
    return sum(
        cash_flow / (1 + wacc) ** year for year, cash_flow in enumerate(cash_flows, 1)
    ) + cash_flows[-1] * (1 + terminal_growth) / (wacc - terminal_growth) / (
        1 + wacc
    ) ** horizon


def _parse_scenarios(
    raw: object,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any], list[str]]:
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValuationError("dcf requires exactly three scenarios")
    scenarios: list[dict[str, Any]] = []
    missing: list[str] = []
    provenance: dict[str, Any] = {}
    source_gaps: list[str] = []
    for index, item in enumerate(raw):
        context = f"dcf scenarios[{index}]"
        if not isinstance(item, dict):
            raise ValuationError(f"{context} requires an object")
        scenario_source = (
            _source(item["source"], context)
            if item.get("source") is not None
            else None
        )
        record: dict[str, Any] = {
            "name": _text(item.get("name"), context),
            "probability": None,
            "rationale": None,
            "free_cash_flows": None,
            "margins": None,
        }
        inputs_provenance: dict[str, Any] = {}
        reinvestment, reinvestment_provenance = _provenance(
            item.get("reinvestment_rate"),
            f"{context} reinvestment_rate",
            inherited_from="scenario",
            fallback_source=scenario_source,
        )
        record["reinvestment_rate"] = reinvestment
        roic, roic_provenance = _provenance(
            item.get("roic"),
            f"{context} roic",
            inherited_from="scenario",
            fallback_source=scenario_source,
        )
        record["roic"] = roic
        if reinvestment_provenance is not None:
            inputs_provenance["reinvestment_rate"] = reinvestment_provenance
        if roic_provenance is not None:
            inputs_provenance["roic"] = roic_provenance
        flows = item.get("free_cash_flows")
        if flows is None:
            missing.append(f"scenarios[{index}].free_cash_flows")
        elif not isinstance(flows, list) or len(flows) < 2:
            raise ValuationError(
                f"{context} requires at least two free-cash-flow values"
            )
        else:
            record["free_cash_flows"] = [
                _number(value, f"{context} free_cash_flows") for value in flows
            ]
            inputs_provenance["free_cash_flows"] = _list_provenance(
                record["free_cash_flows"], scenario_source, "scenario"
            )
        probability = item.get("probability")
        probability_value, probability_provenance = _provenance(
            probability,
            f"{context} probability",
            inherited_from="scenario",
            fallback_source=scenario_source,
        )
        if probability_value is None:
            missing.append(f"scenarios[{index}].probability")
        else:
            record["probability"] = probability_value
            assert probability_provenance is not None
            inputs_provenance["probability"] = probability_provenance
            if probability_provenance["source"] is None:
                source_gaps.append(f"scenarios[{index}].probability")
        if isinstance(probability, dict):
            if probability.get("rationale") is None:
                missing.append(f"scenarios[{index}].probability.rationale")
            else:
                record["rationale"] = _guarded_text(
                    probability["rationale"], f"{context} probability rationale"
                )
        margins = item.get("margins")
        if margins is not None:
            if not isinstance(margins, list) or not margins:
                raise ValuationError(f"{context} margins requires a non-empty list")
            record["margins"] = [
                _number(value, f"{context} margins") for value in margins
            ]
            inputs_provenance["margins"] = _list_provenance(
                record["margins"], scenario_source, "scenario"
            )
        provenance[record["name"]] = inputs_provenance
        scenarios.append(record)
    return scenarios, missing, provenance, source_gaps


def _terminal_checks(
    fields: dict[str, float], scenarios: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    growth = fields["terminal_growth"]
    cap = fields["long_run_growth_cap"]
    benchmark = fields["mature_margin_benchmark"]
    checks: dict[str, Any] = {}
    if growth > cap:
        checks["long_run_growth"] = {
            "status": "fail",
            "detail": f"永续增长率 {growth:.4f} 高于长期增长上限 {cap:.4f}。",
        }
    elif growth > 0.9 * cap:
        checks["long_run_growth"] = {
            "status": "warn",
            "detail": f"永续增长率 {growth:.4f} 接近长期增长上限 {cap:.4f}（超过 90%）。",
        }
    else:
        checks["long_run_growth"] = {
            "status": "pass",
            "detail": f"永续增长率 {growth:.4f} 低于长期增长上限 {cap:.4f}。",
        }
    terminal_margins = [s["margins"][-1] for s in scenarios if s["margins"]]
    if len(terminal_margins) != len(scenarios):
        checks["mature_margin"] = {
            "status": "warn",
            "detail": "至少一个情景未提供 margins，无法完整对照成熟利润率基准。",
        }
    else:
        peak = max(terminal_margins)
        status = "pass"
        if peak > benchmark * 1.25:
            status = "fail"
        elif peak > benchmark:
            status = "warn"
        checks["mature_margin"] = {
            "status": status,
            "detail": f"终值隐含成熟利润率峰值 {peak:.4f} 对照基准 {benchmark:.4f}。",
        }
    deviations: list[float] = []
    incomplete = False
    for scenario in scenarios:
        reinvestment = scenario["reinvestment_rate"]
        roic = scenario["roic"]
        if reinvestment is None or not roic:
            incomplete = True
        else:
            deviations.append(abs(reinvestment - growth / roic))
    if incomplete or not deviations:
        checks["reinvestment_roic_consistency"] = {
            "status": "warn",
            "detail": "至少一个情景缺少 reinvestment_rate 或 roic，无法完整核对再投资率≈g/ROIC。",
        }
    else:
        worst = max(deviations)
        status = "pass" if worst <= 0.05 else "warn" if worst <= 0.15 else "fail"
        checks["reinvestment_roic_consistency"] = {
            "status": status,
            "detail": f"再投资率与 g/ROIC 的最大偏差 {worst:.4f}（pass 容差 0.05）。",
        }
    gaps = [
        f"dcf 终值检查 {key} 判定 fail：{entry['detail']}"
        for key, entry in checks.items()
        if entry["status"] == "fail"
    ]
    return checks, gaps


def _run_dcf(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    fields, provenance = _inputs_provenance(spec, DCF_REQUIRED, "dcf")
    missing = [name for name, value in fields.items() if value is None]
    scenarios: list[dict[str, Any]] = []
    scenario_provenance: dict[str, Any] = {}
    source_gaps: list[str] = []
    raw_scenarios = spec.get("scenarios")
    if raw_scenarios is None:
        missing.append("scenarios")
    else:
        scenarios, scenario_missing, scenario_provenance, source_gaps = (
            _parse_scenarios(raw_scenarios)
        )
        missing.extend(scenario_missing)
    if missing:
        return _missing("dcf", missing)
    names = [scenario["name"] for scenario in scenarios]
    if len(set(names)) != 3 or set(names) != set(SCENARIO_NAMES):
        return _invalid("dcf", "情景必须为 bear、base、bull 各一个且名称唯一。")
    out_of_range = [
        scenario["name"]
        for scenario in scenarios
        if not 0 <= scenario["probability"] <= 1
    ]
    if out_of_range:
        return _invalid(
            "dcf",
            f"情景概率必须落在 [0, 1] 区间，越界情景：{', '.join(out_of_range)}，模型拒绝运行。",
        )
    total_probability = sum(scenario["probability"] for scenario in scenarios)
    if abs(total_probability - 1.0) > PROBABILITY_TOLERANCE:
        return _invalid(
            "dcf",
            f"情景概率合计 {total_probability:.6f}，超出 1±1e-6 的容差，模型拒绝运行。",
        )
    price = fields["price"]
    shares = fields["shares_outstanding"]
    net_debt = fields["net_debt"]
    wacc = fields["wacc"]
    growth = fields["terminal_growth"]
    if price <= 0 or shares <= 0 or not 0 < growth < wacc < 1:
        return _invalid(
            "dcf", "要求 price/shares_outstanding 为正且 0 < terminal_growth < WACC < 1。"
        )
    scenarios.sort(key=lambda scenario: SCENARIO_NAMES.index(scenario["name"]))
    computed: list[dict[str, Any]] = []
    for scenario in scenarios:
        enterprise, equity = _dcf_equity(
            scenario["free_cash_flows"], wacc, growth, net_debt
        )
        computed.append(
            {
                "name": scenario["name"],
                "probability": scenario["probability"],
                "enterprise_value": _round6(enterprise),
                "equity_value": _round6(equity),
                "per_share": _round6(equity / shares),
            }
        )
    weighted = sum(
        scenario["probability"] * entry["per_share"]
        for scenario, entry in zip(scenarios, computed)
    )
    checks, gaps = _terminal_checks(fields, scenarios)
    bear_per_share = next(
        entry["per_share"] for entry in computed if entry["name"] == "bear"
    )
    source_gaps = [
        name
        for name in DCF_KEY_SOURCE_REQUIRED
        if provenance[name]["source"] is None
    ] + source_gaps
    result = {
        "status": "computed",
        "scenarios": computed,
        "probability_weighted_per_share": _round6(weighted),
        "terminal_value_checks": checks,
        "value_zone": {"low": _round6(bear_per_share), "high": _round6(weighted)},
        "inputs_provenance": {
            **provenance,
            "scenarios": {
                scenario["name"]: scenario_provenance[scenario["name"]]
                for scenario in scenarios
            },
        },
    }
    if source_gaps:
        result["source_gaps"] = source_gaps
        gaps.extend(
            f"dcf: 关键输入 {name} 缺少来源（source），模型已照算并在此标注来源缺口。"
            for name in source_gaps
        )
    return result, gaps


def _parse_driver_scenarios(
    raw: object,
) -> tuple[list[dict[str, Any]], list[str], list[str], dict[str, Any], list[str]]:
    """Parse driver_model scenarios; fail closed on any structural break.

    Returns (scenarios, missing, violations, provenance, source_gaps).  Arrays
    must match forecast_periods exactly; nothing is padded or guessed.
    """
    if not isinstance(raw, list) or not raw:
        raise ValuationError("dcf driver_model scenarios requires a non-empty list")
    scenarios: list[dict[str, Any]] = []
    missing: list[str] = []
    violations: list[str] = []
    provenance: dict[str, Any] = {}
    source_gaps: list[str] = []
    for index, item in enumerate(raw):
        context = f"driver_model scenarios[{index}]"
        if not isinstance(item, dict):
            raise ValuationError(f"{context} requires an object")
        scenario_source = (
            _source(item["source"], context)
            if item.get("source") is not None
            else None
        )
        record: dict[str, Any] = {
            "name": _text(item.get("name"), context),
            "probability": None,
            "rationale": None,
            "forecast_periods": None,
            "drivers": None,
            "margins": None,
            "source": scenario_source,
        }
        inputs_provenance: dict[str, Any] = {}
        probability = item.get("probability")
        probability_value, probability_provenance = _provenance(
            probability,
            f"{context} probability",
            inherited_from="scenario",
            fallback_source=scenario_source,
        )
        if probability_value is None:
            missing.append(f"scenarios[{index}].probability")
        else:
            record["probability"] = probability_value
            assert probability_provenance is not None
            inputs_provenance["probability"] = probability_provenance
            if probability_provenance["source"] is None:
                source_gaps.append(f"driver_model.scenarios[{index}].probability")
        if isinstance(probability, dict):
            if probability.get("rationale") is None:
                missing.append(f"scenarios[{index}].probability.rationale")
            else:
                record["rationale"] = _guarded_text(
                    probability["rationale"], f"{context} probability rationale"
                )
        periods_raw = item.get("forecast_periods")
        if periods_raw is None:
            missing.append(f"scenarios[{index}].forecast_periods")
        else:
            periods_value = _number(periods_raw, f"{context} forecast_periods")
            if (
                not periods_value.is_integer()
                or not 1 <= periods_value <= MAX_FORECAST_PERIODS
            ):
                raise ValuationError(
                    f"{context} forecast_periods must be an integer in "
                    f"1..{MAX_FORECAST_PERIODS}"
                )
            record["forecast_periods"] = int(periods_value)
        drivers: dict[str, list[float]] = {}
        if record["forecast_periods"] is not None:
            periods = record["forecast_periods"]
            for field in DRIVER_ARRAY_FIELDS:
                raw_array = item.get(field)
                if raw_array is None:
                    missing.append(f"scenarios[{index}].{field}")
                    continue
                if not isinstance(raw_array, list) or len(raw_array) != periods:
                    raise ValuationError(
                        f"{context} {field} requires a list of exactly "
                        f"{periods} values (forecast_periods)"
                    )
                values = [_number(value, f"{context} {field}") for value in raw_array]
                drivers[field] = values
                inputs_provenance[field] = _list_provenance(
                    values, scenario_source, "scenario"
                )
            if len(drivers) == len(DRIVER_ARRAY_FIELDS):
                record["drivers"] = drivers
                # _terminal_checks 对照成熟利润率基准时使用终值年经营利润率。
                record["margins"] = drivers["operating_margin"]
                if any(value < 0 for value in drivers["revenue"]):
                    violations.append(f"scenarios[{index}].revenue 存在负值")
                if any(
                    not -1 <= value <= 1 for value in drivers["operating_margin"]
                ):
                    violations.append(
                        f"scenarios[{index}].operating_margin 超出 [-1, 1]"
                    )
                if any(not 0 <= value < 1 for value in drivers["tax_rate"]):
                    violations.append(f"scenarios[{index}].tax_rate 超出 [0, 1)")
        for required in ("reinvestment_rate", "roic"):
            value, entry_provenance = _provenance(
                item.get(required),
                f"{context} {required}",
                inherited_from="scenario",
                fallback_source=scenario_source,
            )
            if value is None:
                missing.append(f"scenarios[{index}].{required}")
            else:
                record[required] = value
                assert entry_provenance is not None
                inputs_provenance[required] = entry_provenance
        provenance[record["name"]] = inputs_provenance
        scenarios.append(record)
    names = [scenario["name"] for scenario in scenarios]
    if len(set(names)) != len(names):
        violations.append("driver_model 情景名称必须唯一")
    return scenarios, missing, violations, provenance, source_gaps


def _driver_quality(
    status: str,
    computed: list[dict[str, Any]],
    checks: dict[str, Any] | None,
    scenarios: list[dict[str, Any]],
    shared_provenance: dict[str, Any],
    weighted: float | None,
    computed_moment: datetime,
) -> dict[str, Any]:
    """Generic applicability gate: usable / conditional / unreliable.

    A driver DCF may only anchor a fundamental target when every check is
    clean; missing key sources, stale share/debt sources, short horizons or
    terminal-check warnings degrade to conditional, and failed terminal
    checks or a non-meaningful terminal value degrade to unreliable.
    """
    if status != "computed":
        return {
            "status": "unreliable",
            "flags": [f"model_status:{status}"],
            "reasons": ["模型未完成计算，不能形成基本面目标。"],
        }
    unreliable: list[str] = []
    conditional: list[str] = []
    flags: list[str] = []
    for name, check in (checks or {}).items():
        if check["status"] == "fail":
            unreliable.append(f"终值检查 {name} 判定 fail：{check['detail']}")
            flags.append(f"terminal_check_fail:{name}")
        elif check["status"] == "warn":
            conditional.append(f"终值检查 {name} 判定 warn：{check['detail']}")
            flags.append(f"terminal_check_warn:{name}")
    for scenario in computed:
        if scenario["free_cash_flows"][-1] <= 0:
            unreliable.append(
                f"情景 {scenario['name']} 终值年 FCF 非正，永续终值公式不适用。"
            )
            flags.append("non_positive_terminal_fcf")
    if weighted is None or weighted <= 0:
        unreliable.append("概率加权每股价值非正，模型输出不具备经济意义。")
        flags.append("non_positive_weighted_value")
    shortest = min(scenario["forecast_periods"] for scenario in scenarios)
    if shortest < MIN_USABLE_FORECAST_PERIODS:
        conditional.append(
            f"显式预测期 {shortest} 年短于 {MIN_USABLE_FORECAST_PERIODS} 年，"
            "高增长/过渡/稳定路径覆盖不足。"
        )
        flags.append("short_forecast_horizon")
    for scenario in scenarios:
        source = scenario.get("source")
        if not isinstance(source, dict):
            conditional.append(f"情景 {scenario['name']} 缺少来源（source）。")
            flags.append(f"scenario_source_missing:{scenario['name']}")
            continue
        age_days = (
            computed_moment - _as_of_moment(
                source["as_of"], f"driver_dcf {scenario['name']} source"
            )
        ).days
        if age_days > STALE_SOURCE_DAYS:
            conditional.append(
                f"情景 {scenario['name']} 来源过时（as_of 距 computed_as_of "
                f"{age_days} 天，超过 {STALE_SOURCE_DAYS} 天）。"
            )
            flags.append(f"scenario_source_stale:{scenario['name']}")
    for key in ("shares_outstanding", "net_debt"):
        record = shared_provenance.get(key)
        source = record.get("source") if isinstance(record, dict) else None
        if not isinstance(source, dict):
            conditional.append(f"关键输入 {key} 缺少来源（source）。")
            flags.append(f"key_source_missing:{key}")
            continue
        age_days = (
            computed_moment - _as_of_moment(source["as_of"], f"driver_dcf {key}")
        ).days
        if age_days > STALE_SOURCE_DAYS:
            conditional.append(
                f"关键输入 {key} 来源过时（as_of 距 computed_as_of "
                f"{age_days} 天，超过 {STALE_SOURCE_DAYS} 天）。"
            )
            flags.append(f"key_source_stale:{key}")
    if unreliable:
        status_value = "unreliable"
        reasons = unreliable + conditional
    elif conditional:
        status_value = "conditional"
        reasons = conditional
    else:
        status_value = "usable"
        reasons = ["全部质量检查通过，可形成基本面参考值。"]
    return {"status": status_value, "flags": flags, "reasons": reasons}


def _driver_missing(missing: list[str]) -> tuple[dict[str, Any], list[str]]:
    result, gaps = _missing("driver_dcf", missing)
    result["model_kind"] = DRIVER_MODEL_KIND
    result["model_version"] = MODEL_VERSION
    result["quality"] = _driver_quality(
        "missing_inputs", [], None, [], {}, None, datetime.now(timezone.utc)
    )
    return result, gaps


def _driver_invalid(detail: str) -> tuple[dict[str, Any], list[str]]:
    result, gaps = _invalid("driver_dcf", detail)
    result["model_kind"] = DRIVER_MODEL_KIND
    result["model_version"] = MODEL_VERSION
    result["quality"] = _driver_quality(
        "invalid_inputs", [], None, [], {}, None, datetime.now(timezone.utc)
    )
    return result, gaps


def _run_driver_dcf(
    spec: dict[str, Any], computed_moment: datetime
) -> tuple[dict[str, Any], list[str]]:
    """Driver-based multi-stage DCF sharing the dcf block's capital inputs.

    FCF paths are derived — NOPAT = revenue × operating_margin ×
    (1 − tax_rate)，FCF = NOPAT + D&A − capex − ΔNWC — so year-by-year margin
    and growth fade express the high-growth / transition / stable stages.
    """
    fields, shared_provenance = _inputs_provenance(
        spec, DRIVER_SHARED_REQUIRED, "driver_dcf"
    )
    missing = [name for name, value in fields.items() if value is None]
    scenarios: list[dict[str, Any]] = []
    scenario_provenance: dict[str, Any] = {}
    source_gaps: list[str] = []
    violations: list[str] = []
    raw_model = spec.get("driver_model")
    if raw_model is None:
        missing.append("driver_model")
    elif not isinstance(raw_model, dict):
        raise ValuationError("dcf driver_model requires an object")
    else:
        raw_scenarios = raw_model.get("scenarios")
        if raw_scenarios is None:
            missing.append("driver_model.scenarios")
        else:
            scenarios, scenario_missing, violations, scenario_provenance, source_gaps = (
                _parse_driver_scenarios(raw_scenarios)
            )
            missing.extend(scenario_missing)
    if missing:
        return _driver_missing(missing)
    if violations:
        return _driver_invalid("、".join(violations) + "，模型拒绝运行。")
    out_of_range = [
        scenario["name"]
        for scenario in scenarios
        if not 0 <= scenario["probability"] <= 1
    ]
    if out_of_range:
        return _driver_invalid(
            f"情景概率必须落在 [0, 1] 区间，越界情景：{', '.join(out_of_range)}，模型拒绝运行。"
        )
    total_probability = sum(scenario["probability"] for scenario in scenarios)
    if abs(total_probability - 1.0) > PROBABILITY_TOLERANCE:
        return _driver_invalid(
            f"情景概率合计 {total_probability:.6f}，超出 1±1e-6 的容差，模型拒绝运行。"
        )
    shares = fields["shares_outstanding"]
    net_debt = fields["net_debt"]
    wacc = fields["wacc"]
    growth = fields["terminal_growth"]
    if shares <= 0 or not 0 < growth < wacc < 1:
        return _driver_invalid(
            "要求 shares_outstanding 为正且 0 < terminal_growth < WACC < 1。"
        )
    computed: list[dict[str, Any]] = []
    for scenario in scenarios:
        drivers = scenario["drivers"]
        periods = scenario["forecast_periods"]
        nopat_path = [
            drivers["revenue"][year]
            * drivers["operating_margin"][year]
            * (1 - drivers["tax_rate"][year])
            for year in range(periods)
        ]
        fcf_path = [
            nopat_path[year]
            + drivers["depreciation_amortization"][year]
            - drivers["capex"][year]
            - drivers["change_in_nwc"][year]
            for year in range(periods)
        ]
        enterprise, equity = _dcf_equity(fcf_path, wacc, growth, net_debt)
        terminal = fcf_path[-1] * (1 + growth) / (wacc - growth)
        pv_terminal = terminal / (1 + wacc) ** periods
        computed.append(
            {
                "name": scenario["name"],
                "probability": scenario["probability"],
                "forecast_periods": periods,
                "drivers": {
                    field: [_round6(value) for value in drivers[field]]
                    for field in DRIVER_ARRAY_FIELDS
                },
                "nopat_path": [_round6(value) for value in nopat_path],
                "free_cash_flows": [_round6(value) for value in fcf_path],
                "terminal_value": _round6(terminal),
                "terminal_value_share_of_enterprise": (
                    _round6(pv_terminal / enterprise) if enterprise else None
                ),
                "enterprise_value": _round6(enterprise),
                "equity_value": _round6(equity),
                "per_share": _round6(equity / shares),
            }
        )
    weighted = sum(
        scenario["probability"] * entry["per_share"]
        for scenario, entry in zip(scenarios, computed)
    )
    checks, gaps = _terminal_checks(fields, scenarios)
    quality = _driver_quality(
        "computed",
        computed,
        checks,
        scenarios,
        shared_provenance,
        weighted,
        computed_moment,
    )
    if quality["status"] != "usable":
        gaps.append(
            f"driver_dcf: 质量门槛判定 {quality['status']}："
            f"{'；'.join(quality['reasons'])}未形成基本面目标。"
        )
    result = {
        "model_kind": DRIVER_MODEL_KIND,
        "model_version": MODEL_VERSION,
        "status": "computed",
        "formula": DRIVER_FORMULA,
        "scenarios": computed,
        "probability_weighted_per_share": _round6(weighted),
        "value_zone": {
            "low": _round6(min(entry["per_share"] for entry in computed)),
            "high": _round6(weighted),
        },
        "terminal_assumptions": {
            "terminal_growth": growth,
            "wacc": wacc,
            "detail": (
                "终值沿用 Gordon 增长：terminal = FCF_n × (1 + g) / (WACC − g)，"
                "按 (1 + WACC)^n 折现；参数与 baseline dcf 共享，"
                "严禁为贴近市场价格反向调整 WACC、g、FCF 或概率。"
            ),
        },
        "terminal_value_checks": checks,
        "quality": quality,
        "inputs_provenance": {
            "shared": shared_provenance,
            "scenarios": {
                scenario["name"]: scenario_provenance[scenario["name"]]
                for scenario in scenarios
            },
        },
    }
    if source_gaps:
        result["source_gaps"] = source_gaps
        gaps.extend(
            f"driver_dcf: 关键输入 {name} 缺少来源（source），模型已照算并在此标注来源缺口。"
            for name in source_gaps
        )
    return result, gaps


def _shared_dcf_entry(dcf_spec: object, name: str) -> float | None:
    if not isinstance(dcf_spec, dict):
        return None
    return _entry(dcf_spec.get(name), f"dcf {name}")


def _run_reverse_dcf(
    spec: dict[str, Any], dcf_spec: object
) -> tuple[dict[str, Any], list[str]]:
    missing: list[str] = []
    shared: dict[str, float | None] = {}
    for name in ("price", "shares_outstanding", "net_debt", "wacc", "terminal_growth"):
        value = _shared_dcf_entry(dcf_spec, name)
        if value is None:
            missing.append(f"dcf.{name}")
        shared[name] = value
    current_fcf, fcf_provenance = _provenance(
        spec.get("current_free_cash_flow"), "reverse_dcf current_free_cash_flow"
    )
    horizon_value, horizon_provenance = _provenance(
        spec.get("horizon_years"), "reverse_dcf horizon_years"
    )
    provenance: dict[str, Any] = {}
    if fcf_provenance is not None:
        provenance["current_free_cash_flow"] = fcf_provenance
    if horizon_provenance is not None:
        provenance["horizon_years"] = horizon_provenance
    if current_fcf is None:
        missing.append("current_free_cash_flow")
    if horizon_value is None:
        missing.append("horizon_years")
    if missing:
        return _missing("reverse_dcf", missing)
    assert horizon_value is not None and current_fcf is not None
    if not horizon_value.is_integer():
        return _invalid("reverse_dcf", "horizon_years 必须为整数。")
    horizon = int(horizon_value)
    price = shared["price"]
    shares = shared["shares_outstanding"]
    net_debt = shared["net_debt"]
    wacc = shared["wacc"]
    growth = shared["terminal_growth"]
    assert (
        price is not None
        and shares is not None
        and net_debt is not None
        and wacc is not None
        and growth is not None
    )
    if (
        current_fcf <= 0
        or not 1 <= horizon <= 20
        or price <= 0
        or shares <= 0
        or not 0 < growth < wacc < 1
    ):
        return _invalid(
            "reverse_dcf",
            "要求 current_free_cash_flow 为正、horizon_years 在 1–20 且 0 < terminal_growth < WACC < 1。",
        )
    target = price * shares + net_debt
    bound_low, bound_high = REVERSE_DCF_GROWTH_BOUNDS
    low_value = _reverse_enterprise_value(current_fcf, horizon, bound_low, wacc, growth)
    high_value = _reverse_enterprise_value(
        current_fcf, horizon, bound_high, wacc, growth
    )
    if not min(low_value, high_value) <= target <= max(low_value, high_value):
        return (
            {
                "status": "no_solution",
                "horizon_years": horizon,
                "detail": (
                    f"反向 DCF 无法在 {bound_low:.2%} 至 {bound_high:.2%} "
                    "的增长区间内匹配当前企业价值。"
                ),
                "inputs_provenance": provenance,
            },
            [],
        )
    low, high = bound_low, bound_high
    for _ in range(REVERSE_DCF_ITERATIONS):
        implied = (low + high) / 2
        enterprise_value = _reverse_enterprise_value(
            current_fcf, horizon, implied, wacc, growth
        )
        if enterprise_value < target:
            low = implied
        else:
            high = implied
    implied_cagr = (low + high) / 2
    return (
        {
            "status": "computed",
            "implied_fcf_cagr": _round6(implied_cagr),
            "horizon_years": horizon,
            "detail": (
                f"以当前价格隐含的 {horizon} 年自由现金流年化增长约 "
                f"{implied_cagr:.2%}（搜索区间 {bound_low:.2%} 至 {bound_high:.2%}）。"
            ),
            "inputs_provenance": provenance,
        },
        [],
    )


def _run_pvgo(
    spec: dict[str, Any], dcf_result: object
) -> tuple[dict[str, Any], list[str]]:
    fields, provenance = _inputs_provenance(spec, PVGO_REQUIRED, "pvgo")
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        return _missing("pvgo", missing)
    normalized_fcf = fields["normalized_free_cash_flow"]
    wacc = fields["wacc"]
    net_debt = fields["net_debt"]
    shares = fields["shares_outstanding"]
    price = fields["price"]
    if normalized_fcf <= 0 or wacc <= 0 or shares <= 0 or price <= 0:
        return _invalid(
            "pvgo", "要求 normalized_free_cash_flow/WACC/shares_outstanding/price 为正。"
        )
    no_growth_per_share = (normalized_fcf / wacc - net_debt) / shares
    pvgo_per_share = price - no_growth_per_share
    pvgo_share = pvgo_per_share / price
    table = [
        {
            "metric": "price",
            "value": _round6(price),
            "detail": "当前价格。",
        },
        {
            "metric": "no_growth_value_per_share",
            "value": _round6(no_growth_per_share),
            "detail": "normalized_free_cash_flow / WACC − net_debt 的零增长近似，按股本摊薄。",
        },
        {
            "metric": "pvgo_per_share",
            "value": _round6(pvgo_per_share),
            "detail": "当前价格中由未来增长预期支撑的部分（price − no_growth_value）。",
        },
        {
            "metric": "pvgo_share_of_price",
            "value": _round6(pvgo_share),
            "detail": "PVGO 占当前价格的比例。",
        },
    ]
    if isinstance(dcf_result, dict) and dcf_result.get("status") == "computed":
        table.append(
            {
                "metric": "dcf_probability_weighted_per_share",
                "value": dcf_result["probability_weighted_per_share"],
                "detail": "三情景概率加权公允价值，与现价及零增长价值对照。",
            }
        )
    return (
        {
            "status": "computed",
            "no_growth_value_per_share": _round6(no_growth_per_share),
            "pvgo_per_share": _round6(pvgo_per_share),
            "pvgo_share_of_price": _round6(pvgo_share),
            "expectations_table": table,
            "inputs_provenance": provenance,
        },
        [],
    )


def _parse_pe_scenarios(
    raw: object,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any], list[str]]:
    """Parse explicit EPS and P/E assumptions without manufacturing earnings."""
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValuationError("pe requires exactly three scenarios")
    scenarios: list[dict[str, Any]] = []
    missing: list[str] = []
    provenance: dict[str, Any] = {}
    source_gaps: list[str] = []
    names: set[str] = set()
    for index, item in enumerate(raw):
        context = f"pe scenarios[{index}]"
        if not isinstance(item, dict):
            raise ValuationError(f"{context} requires an object")
        scenario_source = (
            _source(item["source"], context) if item.get("source") is not None else None
        )
        name = _text(item.get("name"), context)
        if name in names:
            raise ValuationError("pe scenario names must be unique")
        names.add(name)
        probability, probability_provenance = _provenance(
            item.get("probability"),
            f"{context} probability",
            inherited_from="scenario",
            fallback_source=scenario_source,
        )
        eps, eps_provenance = _provenance(
            item.get("eps"),
            f"{context} eps",
            inherited_from="scenario",
            fallback_source=scenario_source,
        )
        multiple, multiple_provenance = _provenance(
            item.get("pe_multiple"),
            f"{context} pe_multiple",
            inherited_from="scenario",
            fallback_source=scenario_source,
        )
        record: dict[str, Any] = {
            "name": name,
            "probability": probability,
            "rationale": None,
            "eps": eps,
            "pe_multiple": multiple,
        }
        inputs_provenance: dict[str, Any] = {}
        if probability is None:
            missing.append(f"scenarios[{index}].probability")
        else:
            assert probability_provenance is not None
            inputs_provenance["probability"] = probability_provenance
            if probability_provenance["source"] is None:
                source_gaps.append(f"scenarios[{index}].probability")
        probability_input = item.get("probability")
        if not isinstance(probability_input, dict) or probability_input.get("rationale") is None:
            missing.append(f"scenarios[{index}].probability.rationale")
        else:
            record["rationale"] = _guarded_text(
                probability_input["rationale"],
                f"{context} probability rationale",
            )
        if eps is None:
            missing.append(f"scenarios[{index}].eps")
        else:
            assert eps_provenance is not None
            inputs_provenance["eps"] = eps_provenance
            if eps_provenance["source"] is None:
                source_gaps.append(f"scenarios[{index}].eps")
        if multiple is None:
            missing.append(f"scenarios[{index}].pe_multiple")
        else:
            assert multiple_provenance is not None
            inputs_provenance["pe_multiple"] = multiple_provenance
            if multiple_provenance["source"] is None:
                source_gaps.append(f"scenarios[{index}].pe_multiple")
        provenance[name] = inputs_provenance
        scenarios.append(record)
    return scenarios, missing, provenance, source_gaps


def _pe_quality(
    basis: str,
    price_provenance: dict[str, Any] | None,
    scenarios: list[dict[str, Any]],
    scenario_provenance: dict[str, Any],
    weighted_eps: float,
    computed_moment: datetime,
) -> dict[str, Any]:
    """Apply generic quality gates for a P/E reference value."""
    reasons: list[str] = []
    flags: list[str] = []
    conditional = False
    if basis == "trailing_eps":
        conditional = True
        reasons.append("trailing EPS 未证明已剔除一次性项目，不能单独形成基本面目标。")
        flags.append("trailing_eps_requires_normalization_review")
    records: list[tuple[str, dict[str, Any] | None]] = [("price", price_provenance)]
    for scenario in scenarios:
        inputs = scenario_provenance.get(scenario["name"], {})
        records.extend(
            (
                f"{scenario['name']}.{field}",
                inputs.get(field) if isinstance(inputs, dict) else None,
            )
            for field in ("eps", "pe_multiple")
        )
        probability_record = inputs.get("probability") if isinstance(inputs, dict) else None
        records.append((f"{scenario['name']}.probability", probability_record))
    for label, record in records:
        if not isinstance(record, dict) or not isinstance(record.get("source"), dict):
            conditional = True
            reasons.append(f"{label} 缺少来源。")
            flags.append(f"source_missing:{label}")
            continue
        source = record["source"]
        source_moment = _as_of_moment(source["as_of"], f"pe {label} source")
        age_days = (computed_moment - source_moment).total_seconds() / 86400
        if age_days > STALE_SOURCE_DAYS:
            conditional = True
            reasons.append(f"{label} 来源距计算时点超过 {STALE_SOURCE_DAYS} 天。")
            flags.append(f"source_stale:{label}")
    if weighted_eps <= 0:
        return {
            "status": "unreliable",
            "reasons": ["概率加权 EPS 非正，P/E 不具备经济意义。"],
            "flags": ["non_positive_weighted_eps"],
        }
    if not reasons:
        reasons.append("前瞻或正常化 EPS、情景 P/E 倍数及其来源完整。")
    return {
        "status": "conditional" if conditional else "usable",
        "reasons": reasons,
        "flags": flags,
    }


def _run_pe(
    spec: dict[str, Any], computed_moment: datetime
) -> tuple[dict[str, Any], list[str]]:
    """Compute a scenario-based P/E reference value from explicit EPS inputs."""
    fields, shared_provenance = _inputs_provenance(spec, ("price",), "pe")
    missing = [name for name, value in fields.items() if value is None]
    basis_raw = spec.get("earnings_basis")
    if basis_raw is None:
        missing.append("earnings_basis")
        basis = None
    else:
        basis = _text(basis_raw, "pe earnings_basis")
        if basis not in PE_EARNINGS_BASES:
            return _invalid(
                "pe",
                "earnings_basis 必须是 trailing_eps、forward_eps 或 normalized_eps。",
            )
    rationale_raw = spec.get("basis_rationale")
    if rationale_raw is None:
        missing.append("basis_rationale")
        basis_rationale = None
    else:
        basis_rationale = _guarded_text(rationale_raw, "pe basis_rationale")
    raw_scenarios = spec.get("scenarios")
    if raw_scenarios is None:
        missing.append("scenarios")
        scenarios: list[dict[str, Any]] = []
        scenario_provenance: dict[str, Any] = {}
        source_gaps: list[str] = []
    else:
        scenarios, scenario_missing, scenario_provenance, source_gaps = _parse_pe_scenarios(
            raw_scenarios
        )
        missing.extend(scenario_missing)
    if missing:
        return _missing("pe", missing)
    assert basis is not None
    price = fields["price"]
    if price is None or price <= 0:
        return _invalid("pe", "price 必须为正数。")
    probabilities = [scenario["probability"] for scenario in scenarios]
    if any(value is None or not 0 <= value <= 1 for value in probabilities):
        return _invalid("pe", "情景概率必须落在 [0, 1] 区间。")
    total_probability = sum(value for value in probabilities if value is not None)
    if abs(total_probability - 1.0) > PROBABILITY_TOLERANCE:
        return _invalid(
            "pe",
            f"情景概率合计 {total_probability:.6f}，超出 1±1e-6 的容差。",
        )
    if any(
        scenario["eps"] is None
        or scenario["eps"] <= 0
        or scenario["pe_multiple"] is None
        or scenario["pe_multiple"] <= 0
        for scenario in scenarios
    ):
        return _invalid(
            "pe",
            "PE 要求每个情景的 EPS 与 P/E 倍数均为正数；亏损或非正 EPS 应标记为 not_applicable。",
        )
    weighted_eps = sum(
        scenario["probability"] * scenario["eps"] for scenario in scenarios
    )
    computed = [
        {
            "name": scenario["name"],
            "probability": scenario["probability"],
            "eps": _round6(scenario["eps"]),
            "pe_multiple": _round6(scenario["pe_multiple"]),
            "per_share": _round6(scenario["eps"] * scenario["pe_multiple"]),
        }
        for scenario in scenarios
    ]
    weighted = sum(
        scenario["probability"] * entry["per_share"]
        for scenario, entry in zip(scenarios, computed)
    )
    quality = _pe_quality(
        basis,
        shared_provenance.get("price"),
        scenarios,
        scenario_provenance,
        weighted_eps,
        computed_moment,
    )
    gaps: list[str] = []
    if quality["status"] != "usable":
        gaps.append(
            f"pe: 质量门槛判定 {quality['status']}："
            f"{'；'.join(quality['reasons'])}"
        )
    if source_gaps:
        gaps.extend(
            f"pe: 关键输入 {name} 缺少来源（source），模型已照算并在此标注来源缺口。"
            for name in source_gaps
        )
    return (
        {
            "model_kind": "pe_multiple",
            "model_version": MODEL_VERSION,
            "method": "pe",
            "status": "computed",
            "earnings_basis": basis,
            "basis_rationale": basis_rationale,
            "scenarios": computed,
            "probability_weighted_eps": _round6(weighted_eps),
            "probability_weighted_per_share": _round6(weighted),
            "value_zone": {
                "low": _round6(min(entry["per_share"] for entry in computed)),
                "high": _round6(weighted),
            },
            "current_price": _round6(price),
            "current_pe": _round6(price / weighted_eps),
            "quality": quality,
            "inputs_provenance": {
                "shared": shared_provenance,
                "scenarios": scenario_provenance,
            },
            **({"source_gaps": source_gaps} if source_gaps else {}),
        },
        gaps,
    )


def _run_epv(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    fields, provenance = _inputs_provenance(spec, EPV_REQUIRED, "epv")
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        return _missing("epv", missing)
    ebit = fields["normalized_ebit"]
    tax_rate = fields["tax_rate"]
    maintenance_capex = fields["maintenance_capex"]
    wacc = fields["wacc"]
    net_debt = fields["net_debt"]
    shares = fields["shares_outstanding"]
    if not 0 <= tax_rate < 1 or wacc <= 0 or shares <= 0:
        return _invalid(
            "epv", "要求 0 <= tax_rate < 1、WACC 与 shares_outstanding 为正。"
        )
    adjusted_earnings = ebit * (1 - tax_rate) - maintenance_capex
    enterprise = adjusted_earnings / wacc
    equity = enterprise - net_debt
    per_share = equity / shares
    return (
        {
            "status": "computed",
            "adjusted_earnings": _round6(adjusted_earnings),
            "enterprise_value": _round6(enterprise),
            "equity_value": _round6(equity),
            "epv_per_share": _round6(per_share),
            "detail": (
                "EPV = (normalized_ebit × (1 − tax_rate) − maintenance_capex) / WACC "
                "− net_debt，按股本摊薄。"
            ),
            "inputs_provenance": provenance,
        },
        [],
    )


def _run_eva(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    fields, provenance = _inputs_provenance(spec, EVA_REQUIRED, "eva")
    missing = [name for name, value in fields.items() if value is None]
    raw_nopat = spec.get("nopat_path")
    nopat_path: list[float] = []
    if raw_nopat is None:
        missing.append("nopat_path")
    elif not isinstance(raw_nopat, list) or not raw_nopat:
        raise ValuationError("eva nopat_path requires a non-empty list")
    else:
        nopat_path = [_number(value, "eva nopat_path") for value in raw_nopat]
        provenance["nopat_path"] = _list_provenance(nopat_path)
    if missing:
        return _missing("eva", missing)
    invested = fields["invested_capital_start"]
    wacc = fields["wacc"]
    growth = fields["terminal_growth"]
    net_debt = fields["net_debt"]
    shares = fields["shares_outstanding"]
    if invested <= 0 or shares <= 0 or not 0 < growth < wacc < 1:
        return _invalid(
            "eva",
            "要求 invested_capital_start/shares_outstanding 为正且 0 < terminal_growth < WACC < 1。",
        )
    capital = invested
    pv_eva = 0.0
    for year, nopat in enumerate(nopat_path, 1):
        pv_eva += (nopat - wacc * capital) / (1 + wacc) ** year
        capital *= 1 + growth
    terminal_eva = nopat_path[-1] * (1 + growth) - wacc * capital
    pv_terminal = terminal_eva / (wacc - growth) / (1 + wacc) ** len(nopat_path)
    firm_value = invested + pv_eva + pv_terminal
    equity = firm_value - net_debt
    per_share = equity / shares
    return (
        {
            "status": "computed",
            "firm_value": _round6(firm_value),
            "equity_value": _round6(equity),
            "residual_income_per_share": _round6(per_share),
            "detail": (
                "剩余收益法：firm = invested_capital₀ + Σ EVA_t/(1+WACC)^t + "
                "EVA_{n+1}/(WACC−g)/(1+WACC)^n，其中 EVA_t = NOPAT_t − WACC × "
                "invested_capital_{t-1}，投入资本按 terminal_growth 外推。"
            ),
            "inputs_provenance": provenance,
        },
        [],
    )


def _run_sotp(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    fields, provenance = _inputs_provenance(spec, SOTP_REQUIRED, "sotp")
    missing = [name for name, value in fields.items() if value is None]
    raw_segments = spec.get("segments")
    segments: list[dict[str, Any]] = []
    segments_provenance: list[dict[str, Any]] = []
    if raw_segments is None:
        missing.append("segments")
    elif not isinstance(raw_segments, list) or not raw_segments:
        raise ValuationError("sotp segments requires a non-empty list")
    else:
        for index, item in enumerate(raw_segments):
            context = f"sotp segments[{index}]"
            if not isinstance(item, dict):
                raise ValuationError(f"{context} requires an object")
            name = _text(item.get("name"), context)
            model = _text(item.get("model"), context)
            if model not in {"dcf", "epv", "multiple"}:
                raise ValuationError(f"{context} model must be dcf, epv, or multiple")
            segment_source = (
                _source(item["source"], context)
                if item.get("source") is not None
                else None
            )
            inputs = item.get("inputs")
            if inputs is None:
                missing.append(f"segments[{index}].inputs")
                continue
            if not isinstance(inputs, dict):
                raise ValuationError(f"{context} inputs requires an object")
            prefix = f"segments[{index}].inputs"

            def segment_entry(
                label: str,
            ) -> tuple[float | None, dict[str, Any] | None]:
                return _provenance(
                    inputs.get(label),
                    f"{prefix} {label}",
                    inherited_from="segment",
                    fallback_source=segment_source,
                )

            inputs_provenance: dict[str, Any] = {}
            enterprise: float | None = None
            if model == "dcf":
                flows = inputs.get("free_cash_flows")
                wacc, wacc_provenance = segment_entry("wacc")
                growth, growth_provenance = segment_entry("terminal_growth")
                if wacc_provenance is not None:
                    inputs_provenance["wacc"] = wacc_provenance
                if growth_provenance is not None:
                    inputs_provenance["terminal_growth"] = growth_provenance
                if flows is None:
                    missing.append(f"{prefix}.free_cash_flows")
                elif not isinstance(flows, list) or len(flows) < 2:
                    raise ValuationError(
                        f"{prefix} requires at least two free-cash-flow values"
                    )
                if wacc is None:
                    missing.append(f"{prefix}.wacc")
                if growth is None:
                    missing.append(f"{prefix}.terminal_growth")
                if (
                    isinstance(flows, list)
                    and len(flows) >= 2
                    and wacc is not None
                    and growth is not None
                ):
                    if not 0 < growth < wacc < 1:
                        return _invalid(
                            "sotp", f"{prefix} 要求 0 < terminal_growth < WACC < 1。"
                        )
                    cash_flows = [
                        _number(value, f"{prefix} free_cash_flows") for value in flows
                    ]
                    inputs_provenance["free_cash_flows"] = _list_provenance(
                        cash_flows, segment_source, "segment"
                    )
                    enterprise = _dcf_equity(cash_flows, wacc, growth, 0.0)[0]
            elif model == "epv":
                ebit, ebit_provenance = segment_entry("normalized_ebit")
                tax_rate, tax_provenance = segment_entry("tax_rate")
                capex, capex_provenance = segment_entry("maintenance_capex")
                wacc, wacc_provenance = segment_entry("wacc")
                for label, value, entry_provenance in (
                    ("normalized_ebit", ebit, ebit_provenance),
                    ("tax_rate", tax_rate, tax_provenance),
                    ("maintenance_capex", capex, capex_provenance),
                    ("wacc", wacc, wacc_provenance),
                ):
                    if entry_provenance is not None:
                        inputs_provenance[label] = entry_provenance
                    if value is None:
                        missing.append(f"{prefix}.{label}")
                if (
                    ebit is not None
                    and tax_rate is not None
                    and capex is not None
                    and wacc is not None
                ):
                    if not 0 <= tax_rate < 1 or wacc <= 0:
                        return _invalid(
                            "sotp", f"{prefix} 要求 0 <= tax_rate < 1 且 WACC 为正。"
                        )
                    enterprise = (ebit * (1 - tax_rate) - capex) / wacc
            else:
                metric, metric_provenance = segment_entry("metric_value")
                multiple, multiple_provenance = segment_entry("multiple")
                if metric_provenance is not None:
                    inputs_provenance["metric_value"] = metric_provenance
                if multiple_provenance is not None:
                    inputs_provenance["multiple"] = multiple_provenance
                if metric is None:
                    missing.append(f"{prefix}.metric_value")
                if multiple is None:
                    missing.append(f"{prefix}.multiple")
                if metric is not None and multiple is not None:
                    enterprise = metric * multiple
            if enterprise is not None:
                segments.append(
                    {
                        "name": name,
                        "model": model,
                        "enterprise_value": _round6(enterprise),
                    }
                )
                segments_provenance.append(
                    {"name": name, "inputs": inputs_provenance}
                )
    if missing:
        return _missing("sotp", missing)
    discount = fields["holding_discount"]
    shares = fields["shares_outstanding"]
    if not 0 <= discount < 1 or shares <= 0:
        return _invalid(
            "sotp", "要求 0 <= holding_discount < 1 且 shares_outstanding 为正。"
        )
    enterprise_total = sum(segment["enterprise_value"] for segment in segments)
    equity = enterprise_total * (1 - discount) - fields["net_debt"]
    return (
        {
            "status": "computed",
            "enterprise_value": _round6(enterprise_total),
            "equity_value": _round6(equity),
            "per_share": _round6(equity / shares),
            "segments": segments,
            "detail": (
                "SOTP = Σ segment_enterprise_value × (1 − holding_discount) "
                "− net_debt，按股本摊薄。"
            ),
            "inputs_provenance": {**provenance, "segments": segments_provenance},
        },
        [],
    )


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = floor(position)
    fraction = position - lower
    if lower + 1 < len(sorted_values):
        return (
            sorted_values[lower] * (1 - fraction)
            + sorted_values[lower + 1] * fraction
        )
    return sorted_values[lower]


def _run_monte_carlo(
    spec: dict[str, Any], dcf_spec: object
) -> tuple[dict[str, Any], list[str]]:
    base_model = _text(spec.get("base_model"), "monte_carlo base_model")
    if base_model != "dcf":
        raise ValuationError("monte_carlo base_model must be dcf")
    missing: list[str] = []
    raw_trials = spec.get("trials")
    raw_seed = spec.get("seed")
    trials_value = (
        None if raw_trials is None else _number(raw_trials, "monte_carlo trials")
    )
    seed_value = None if raw_seed is None else _number(raw_seed, "monte_carlo seed")
    if trials_value is None:
        missing.append("trials")
    if seed_value is None:
        missing.append("seed")
    distributions = spec.get("distributions")
    ranges: dict[str, tuple[float, float]] = {}
    distribution_provenance: dict[str, Any] = {}
    if distributions is None:
        missing.append("distributions")
    elif not isinstance(distributions, dict):
        raise ValuationError("monte_carlo distributions requires an object")
    else:
        for key in ("wacc", "terminal_growth", "fcf_growth"):
            entry = distributions.get(key)
            if entry is None:
                missing.append(f"distributions.{key}")
                continue
            if not isinstance(entry, dict):
                raise ValuationError(
                    f"monte_carlo distributions.{key} requires an object"
                )
            low = _number(entry.get("low"), f"monte_carlo distributions.{key}.low")
            high = _number(entry.get("high"), f"monte_carlo distributions.{key}.high")
            if not low < high:
                raise ValuationError(
                    f"monte_carlo distributions.{key} requires low < high"
                )
            ranges[key] = (low, high)
            distribution_provenance[key] = {
                "low": low,
                "high": high,
                "source": (
                    _source(entry["source"], f"monte_carlo distributions.{key}")
                    if entry.get("source") is not None
                    else None
                ),
            }
    net_debt = _shared_dcf_entry(dcf_spec, "net_debt")
    shares = _shared_dcf_entry(dcf_spec, "shares_outstanding")
    if net_debt is None:
        missing.append("dcf.net_debt")
    if shares is None:
        missing.append("dcf.shares_outstanding")
    base_flows: list[float] | None = None
    raw_scenarios = dcf_spec.get("scenarios") if isinstance(dcf_spec, dict) else None
    if isinstance(raw_scenarios, list):
        for item in raw_scenarios:
            if isinstance(item, dict) and item.get("name") == "base":
                flows = item.get("free_cash_flows")
                if isinstance(flows, list) and flows:
                    base_flows = [
                        _number(value, "dcf base free_cash_flows") for value in flows
                    ]
    if base_flows is None:
        missing.append("dcf.scenarios[base].free_cash_flows")
    if missing:
        return _missing("monte_carlo", missing)
    assert trials_value is not None and seed_value is not None
    if not trials_value.is_integer() or trials_value < 1:
        return _invalid("monte_carlo", "trials 必须为正整数。")
    if not seed_value.is_integer():
        return _invalid("monte_carlo", "seed 必须为整数。")
    assert net_debt is not None and shares is not None and base_flows is not None
    if shares <= 0:
        return _invalid("monte_carlo", "要求 dcf.shares_outstanding 为正。")
    trials = int(trials_value)
    seed = int(seed_value)
    rng = random.Random(seed)
    values: list[float] = []
    rejected = 0
    horizon = len(base_flows)
    for _ in range(trials):
        wacc = rng.uniform(*ranges["wacc"])
        growth = rng.uniform(*ranges["terminal_growth"])
        fcf_growth = rng.uniform(*ranges["fcf_growth"])
        if not wacc > growth:
            rejected += 1
            continue
        flows = [base_flows[0] * (1 + fcf_growth) ** year for year in range(1, horizon + 1)]
        _, equity = _dcf_equity(flows, wacc, growth, net_debt)
        values.append(equity / shares)
    if not values:
        return _invalid(
            "monte_carlo", "所有抽样均不满足 WACC > terminal_growth，无法生成分布。"
        )
    values.sort()
    return (
        {
            "status": "computed",
            "base_model": "dcf",
            "seed": seed,
            "trials": trials,
            "accepted_draws": len(values),
            "rejected_draws": rejected,
            "percentiles": {
                "p10": _round6(_percentile(values, 0.10)),
                "p50": _round6(_percentile(values, 0.50)),
                "p90": _round6(_percentile(values, 0.90)),
            },
            "inputs_provenance": {"distributions": distribution_provenance},
        },
        [],
    )


def _identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValuationError("identity requires an object")
    record: dict[str, Any] = {
        field: _text(value.get(field), f"identity {field}")
        for field in ("issuer_id", "listing_id", "case_id")
    }
    for field in ("artifact_version", "schema_version"):
        number = value.get(field)
        if not _is_version_one(number):
            raise ValuationError(f"identity {field} must be 1")
        record[field] = number
    return record


def compute_valuation(fixture: dict[str, Any]) -> dict[str, Any]:
    schema_version = fixture.get("schema_version")
    if not _is_version_one(schema_version):
        raise ValuationError("fixture schema_version must be 1")
    identity = _identity(fixture.get("identity"))
    computed_as_of = _as_of(fixture.get("computed_as_of"), "fixture")
    computed_moment = _as_of_moment(fixture.get("computed_as_of"), "fixture")
    market_scope = _text(fixture.get("market_scope"), "fixture market_scope")
    if market_scope not in MARKET_SCOPES:
        raise ValuationError(
            f"fixture market_scope is not supported: {market_scope}"
        )
    currency = _text(fixture.get("currency"), "fixture currency")
    if currency not in CURRENCIES:
        raise ValuationError(f"fixture currency is not supported: {currency}")
    models = fixture.get("models")
    if not isinstance(models, dict):
        raise ValuationError("fixture models requires an object")
    _check_input_currencies(models, currency)
    dcf_spec = models.get("dcf")
    results: dict[str, Any] = {}
    gaps: list[str] = []
    for name in MODEL_ORDER:
        spec = models.get(name)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            raise ValuationError(f"{name} requires an object")
        status = spec.get("status")
        if status == "not_applicable":
            results[name] = {
                "status": "not_applicable",
                "reason": _guarded_text(spec.get("reason"), f"{name} reason"),
            }
            continue
        if status != "requested":
            raise ValuationError(f"{name} status must be requested or not_applicable")
        if name == "dcf":
            result, model_gaps = _run_dcf(spec)
            # 旧三情景 DCF 保留为可审计 baseline；其现金流路径为直接给定、
            # 未由经营驱动推导，按通用质量门槛不构成基本面目标。
            result["model_role"] = "baseline"
        elif name == "reverse_dcf":
            result, model_gaps = _run_reverse_dcf(spec, dcf_spec)
        elif name == "pvgo":
            result, model_gaps = _run_pvgo(spec, results.get("dcf"))
        elif name == "pe":
            result, model_gaps = _run_pe(spec, computed_moment)
        elif name == "epv":
            result, model_gaps = _run_epv(spec)
        elif name == "eva":
            result, model_gaps = _run_eva(spec)
        elif name == "sotp":
            result, model_gaps = _run_sotp(spec)
        else:
            result, model_gaps = _run_monte_carlo(spec, dcf_spec)
        results[name] = result
        gaps.extend(model_gaps)
        if name == "dcf" and spec.get("driver_model") is not None:
            driver_result, driver_gaps = _run_driver_dcf(spec, computed_moment)
            results["driver_dcf"] = driver_result
            gaps.extend(driver_gaps)
    _check_source_times(results, computed_moment, "results")
    artifact: dict[str, Any] = {
        "identity": identity,
        "schema_version": 1,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "model_version": MODEL_VERSION,
        "computed_as_of": computed_as_of,
        "market_scope": market_scope,
        "currency": currency,
        "results": results,
        "data_gaps": gaps,
    }
    # A/H 配对披露与 VIE/ADR 识别块原样透传给下游（trade_plan），
    # 由消费方按 market_contracts.json 的合同校验；本引擎只做对象形状检查。
    for passthrough in ("ah_compare", "vie_adr"):
        block = fixture.get(passthrough)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ValuationError(f"{passthrough} requires an object")
        artifact[passthrough] = block
    return artifact


def _write_new(path: Path, artifact: dict[str, Any]) -> None:
    resolved = path.resolve()
    if RUNTIME_ROOT == resolved or RUNTIME_ROOT in resolved.parents:
        raise ValuationError("output path must not be inside the Skill runtime package")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(artifact, output, ensure_ascii=False, indent=2)
        output.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        fixture = json.loads(arguments.input.read_text(encoding="utf-8"))
        if not isinstance(fixture, dict):
            raise ValuationError("fixture must be a JSON object")
        _write_new(arguments.output, compute_valuation(fixture))
    except (OSError, json.JSONDecodeError, ValuationError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
