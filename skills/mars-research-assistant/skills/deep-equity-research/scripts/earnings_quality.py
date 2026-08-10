#!/usr/bin/env python3
"""Score earnings quality (A-D) from versioned, auditable rules."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any


class EarningsQualityError(ValueError):
    """Fail closed instead of fabricating an earnings-quality conclusion."""


RUNTIME_ROOT = Path(__file__).resolve().parents[3]
RULES_PATH = (
    Path(__file__).resolve().parents[1] / "reference" / "earnings_quality_rules.json"
)
MARKET_CONTRACTS_PATH = (
    Path(__file__).resolve().parents[1] / "reference" / "market_contracts.json"
)
ENGINE = "skills/deep-equity-research/scripts/earnings_quality.py"
ENGINE_VERSION = "1.0.0"
EXPECTED_RULES_VERSION = "v1.0.3-eq-1"
MARKET_SCOPES = {"us", "hk", "a_share", "ah_compare"}
CURRENCIES = {"USD", "HKD", "CNY"}
SOURCE_KINDS = {
    "sec_filing",
    "regulatory_filing",
    "issuer_ir",
    "exchange",
    "issuer_announcement",
    "credible_media",
}
COMPONENT_ORDER = (
    "accruals",
    "beneish",
    "revenue_recognition",
    "cash_flow",
    "audit_governance",
)
SHORT_ADVICE = re.compile(r"做空|沽空|卖空|\bshort\b", re.IGNORECASE)
MIN_ANNUAL_YEARS = 3
MIN_QUARTERS = 8


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EarningsQualityError(f"{context} requires text")
    return value.strip()


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EarningsQualityError(f"{context} requires a finite number")
    result = float(value)
    if not isfinite(result):
        raise EarningsQualityError(f"{context} requires a finite number")
    return result


def _round(value: float) -> float:
    return round(value, 6)


def _as_of(value: object, context: str) -> str:
    text = _text(value, context)
    if "T" not in text:
        raise EarningsQualityError(
            f"{context} requires a complete timestamp with timezone"
        )
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as error:
        raise EarningsQualityError(f"{context} requires an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EarningsQualityError(f"{context} timestamp requires a timezone")
    return text


def _as_of_moment(value: object, context: str) -> datetime:
    text = _as_of(value, context)
    parsed = datetime.fromisoformat(
        text[:-1] + "+00:00" if text.endswith("Z") else text
    )
    return parsed.astimezone(timezone.utc)


def _check_source_times(node: object, computed: datetime, context: str) -> None:
    """Fail closed when any source postdates computed_as_of; no future data."""
    if isinstance(node, dict):
        if {"name", "kind", "as_of", "url"}.issubset(node):
            moment = _as_of_moment(node["as_of"], f"{context} source")
            if moment > computed:
                raise EarningsQualityError(
                    f"{context} source as_of is after computed_as_of: "
                    f"{node['as_of']}"
                )
        for key, value in node.items():
            _check_source_times(value, computed, f"{context}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_source_times(value, computed, f"{context}[{index}]")


def _source(value: object, context: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EarningsQualityError(f"{context} requires a source")
    name = _text(value.get("name"), f"{context} source")
    kind = _text(value.get("kind"), f"{context} source")
    if kind not in SOURCE_KINDS:
        raise EarningsQualityError(f"{context} source kind is not allowed: {kind}")
    as_of = _as_of(value.get("as_of"), f"{context} source")
    url = _text(value.get("url"), f"{context} source")
    return {"name": name, "kind": kind, "as_of": as_of, "url": url}


def _identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EarningsQualityError("identity requires an object")
    record: dict[str, Any] = {
        field: _text(value.get(field), f"identity {field}")
        for field in ("issuer_id", "listing_id", "case_id")
    }
    for field in ("artifact_version", "schema_version"):
        version = value.get(field)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise EarningsQualityError(f"identity {field} must be 1")
        record[field] = version
    return record


def _load_rules() -> dict[str, Any]:
    try:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EarningsQualityError(f"cannot load rules file: {error}") from error
    if not isinstance(rules, dict) or rules.get("rules_version") != EXPECTED_RULES_VERSION:
        raise EarningsQualityError(
            f"rules file version must be {EXPECTED_RULES_VERSION}"
        )
    return rules


def _load_market_scopes() -> dict[str, Any]:
    try:
        contracts = json.loads(MARKET_CONTRACTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EarningsQualityError(f"cannot load market contracts: {error}") from error
    scopes = contracts.get("scopes") if isinstance(contracts, dict) else None
    if not isinstance(scopes, dict):
        raise EarningsQualityError("market contracts scopes must be an object")
    return scopes


def _check_market_contract(
    scopes: dict[str, Any], market_scope: str, currency: str, listing_id: str
) -> None:
    """scope→currency and listing-suffix checks per market_contracts.json."""
    if market_scope == "ah_compare":
        declared_suffixes = tuple(
            suffix
            for scope in ("a_share", "hk")
            for suffix in scopes[scope]["suffixes"]
            if suffix
        )
        allowed_currencies = {
            scopes["a_share"]["currency"], scopes["hk"]["currency"]
        }
        allows_bare = False
    else:
        contract = scopes[market_scope]
        declared_suffixes = tuple(
            suffix for suffix in contract["suffixes"] if suffix
        )
        allowed_currencies = {contract["currency"]}
        allows_bare = "" in contract["suffixes"]
    if currency not in allowed_currencies:
        allowed = "/".join(sorted(allowed_currencies))
        raise EarningsQualityError(
            f"market_scope {market_scope} does not allow currency {currency} "
            f"(expected {allowed})"
        )
    if declared_suffixes and listing_id.endswith(declared_suffixes):
        return
    if allows_bare:
        foreign_suffixes = tuple(
            suffix
            for name, contract in scopes.items()
            if name != "ah_compare"
            for suffix in contract.get("suffixes", [])
            if suffix and suffix not in declared_suffixes
        )
        if not listing_id.endswith(foreign_suffixes):
            return
    raise EarningsQualityError(
        f"listing_id {listing_id} suffix does not match market_scope {market_scope}"
    )


def _band_deduction(value: float, bands: list[dict[str, Any]]) -> tuple[float, str | None]:
    for band in sorted(bands, key=lambda item: -float(item["above"])):
        if value > float(band["above"]):
            return float(band["deduct"]), str(band["reason"])
    return 0.0, None


def _component_section(components: dict[str, Any], key: str) -> dict[str, Any] | None:
    section = components.get(key)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise EarningsQualityError(f"components.{key} must be an object")
    return section


def _accounting_periods(value: object) -> tuple[dict[str, Any], list[str]]:
    """Record the annual/quarterly baseline; shortfall degrades to provisional."""
    if value is None:
        return (
            {
                "status": "missing",
                "annual_count": 0,
                "annual": [],
                "quarter_count": 0,
                "quarters": [],
            },
            [
                "accounting_periods：未提供会计期间基线"
                f"（年度 ≥{MIN_ANNUAL_YEARS} 年、季度 ≥{MIN_QUARTERS} 个），基线按缺口处理。"
            ],
        )
    if not isinstance(value, dict):
        raise EarningsQualityError("accounting_periods requires an object")

    def _labels(*keys: str) -> list[str]:
        # Accept the contract name first and the legacy alias second; providing
        # both at once is ambiguous and must fail closed.
        provided = [key for key in keys if value.get(key) is not None]
        if len(provided) > 1:
            raise EarningsQualityError(
                f"accounting_periods must not provide both {' and '.join(provided)}"
            )
        if not provided:
            return []
        key = provided[0]
        raw = value[key]
        if not isinstance(raw, list):
            raise EarningsQualityError(f"accounting_periods {key} requires a list")
        labels: list[str] = []
        for index, entry in enumerate(raw):
            context = f"accounting_periods {key}[{index}]"
            if isinstance(entry, dict):
                labels.append(_text(entry.get("period"), context))
            else:
                labels.append(_text(entry, context))
        return labels

    annual = _labels("annual_years", "annual")
    quarters = _labels("quarters")
    gaps: list[str] = []
    if len(annual) < MIN_ANNUAL_YEARS:
        gaps.append(
            f"accounting_periods：年度基线仅 {len(annual)} 年，"
            f"低于 {MIN_ANNUAL_YEARS} 年要求。"
        )
    if len(quarters) < MIN_QUARTERS:
        gaps.append(
            f"accounting_periods：季度基线仅 {len(quarters)} 个，"
            f"低于 {MIN_QUARTERS} 个要求。"
        )
    return (
        {
            "status": "recorded",
            "annual_count": len(annual),
            "annual": annual,
            "quarter_count": len(quarters),
            "quarters": quarters,
        },
        gaps,
    )


def _accruals(
    section: dict[str, Any] | None, rules: dict[str, Any], gaps: list[str]
) -> dict[str, Any]:
    if section is None:
        gaps.append("accruals：未提供年度净利润/经营现金流/总资产序列。")
        return {"status": "missing"}
    policy = rules["accruals"]
    annual = section.get("annual")
    if not isinstance(annual, list) or len(annual) < int(policy["min_years"]):
        gaps.append(
            f"accruals：年度序列不足 {policy['min_years']} 年，组件记 missing。"
        )
        return {"status": "missing", "reason": "insufficient annual series"}
    base_source = _source(section.get("source"), "accruals")
    rows: list[dict[str, Any]] = []
    evidence: list[str] = []
    deduct = 0.0
    for entry in annual:
        if not isinstance(entry, dict):
            raise EarningsQualityError("accruals annual entry must be an object")
        period = _text(entry.get("period"), "accruals period")
        net_income = _number(entry.get("net_income"), f"{period} net_income")
        ocf = _number(entry.get("operating_cash_flow"), f"{period} operating_cash_flow")
        assets = _number(entry.get("total_assets"), f"{period} total_assets")
        if assets <= 0:
            raise EarningsQualityError(f"{period} total_assets must be positive")
        source = (
            _source(entry["source"], f"{period} accruals")
            if "source" in entry
            else base_source
        )
        ratio = (net_income - ocf) / assets
        hit, reason = _band_deduction(ratio, policy["annual_deductions"])
        deduct += hit
        rows.append(
            {
                "period": period,
                "net_income": net_income,
                "operating_cash_flow": ocf,
                "total_assets": assets,
                "accrual_ratio": _round(ratio),
                "deduction": hit,
                "source": source,
            }
        )
        note = f"，触发扣分 {hit:g}（{reason}）" if hit else ""
        evidence.append(
            f"{period} 应计比率 {_round(ratio)} = ({net_income:g} - {ocf:g}) / {assets:g}"
            f"{note}（来源：{source['name']}，as_of：{source['as_of']}）"
        )
    mean_ratio = sum(row["accrual_ratio"] for row in rows) / len(rows)
    mean_hit, mean_reason = _band_deduction(mean_ratio, policy["mean_deductions"])
    deduct += mean_hit
    if mean_hit:
        evidence.append(f"多年均值应计比率 {_round(mean_ratio)}，触发扣分 {mean_hit:g}（{mean_reason}）。")
    else:
        evidence.append(f"多年均值应计比率 {_round(mean_ratio)}，未触发扣分。")
    return {
        "status": "computed",
        "score": _round(max(0.0, 100.0 - deduct)),
        "annual_ratios": rows,
        "mean_accrual_ratio": _round(mean_ratio),
        "evidence": evidence,
    }


def _beneish(
    section: dict[str, Any] | None, rules: dict[str, Any], gaps: list[str]
) -> dict[str, Any]:
    if section is None:
        gaps.append("beneish：未提供两年可比财务报表字段。")
        return {"status": "missing"}
    policy = rules["beneish"]
    if section.get("comparable") is not True:
        reason = _text(
            section.get("reason") or "两年会计口径不可比", "beneish comparability"
        )
        gaps.append(f"beneish：{reason}，组件记 not_applicable。")
        return {"status": "not_applicable", "reason": reason}
    required = [str(field) for field in policy["required_fields"]]
    blocks: dict[str, dict[str, float]] = {}
    for label in ("current", "prior"):
        block = section.get(label)
        if not isinstance(block, dict):
            reason = f"beneish {label} period block is missing"
            gaps.append(f"beneish：{reason}，组件记 not_applicable。")
            return {"status": "not_applicable", "reason": reason}
        missing = [field for field in required if field not in block]
        if missing:
            reason = f"beneish {label} 缺少字段：{', '.join(missing)}"
            gaps.append(f"beneish：{reason}，组件记 not_applicable。")
            return {"status": "not_applicable", "reason": reason}
        blocks[label] = {
            field: _number(block[field], f"beneish {label} {field}") for field in required
        }
    cur, pri = blocks["current"], blocks["prior"]
    # Every denominator in the M-Score chain — raw fields and intermediate
    # ratios alike — must be non-zero; any zero degrades the component to
    # not_applicable with a data gap instead of raising ZeroDivisionError.
    zero: list[str] = []

    def _ratio(numerator: float, denominator: float, name: str) -> float:
        if denominator == 0:
            if name not in zero:
                zero.append(name)
            return 0.0
        return numerator / denominator

    dsri_current = _ratio(cur["receivables"], cur["revenue"], "current revenue")
    dsri_prior = _ratio(pri["receivables"], pri["revenue"], "prior revenue")
    gmi_current = _ratio(
        cur["revenue"] - cur["cogs"], cur["revenue"], "current revenue"
    )
    gmi_prior = _ratio(pri["revenue"] - pri["cogs"], pri["revenue"], "prior revenue")
    aqi_current_term = 1 - _ratio(
        cur["current_assets"] + cur["net_ppe"],
        cur["total_assets"],
        "current total_assets",
    )
    aqi_prior_term = 1 - _ratio(
        pri["current_assets"] + pri["net_ppe"], pri["total_assets"], "prior total_assets"
    )
    depi_current = _ratio(
        cur["depreciation"],
        cur["depreciation"] + cur["net_ppe"],
        "current depreciation + net_ppe",
    )
    depi_prior = _ratio(
        pri["depreciation"],
        pri["depreciation"] + pri["net_ppe"],
        "prior depreciation + net_ppe",
    )
    sgai_current = _ratio(cur["sga_expense"], cur["revenue"], "current revenue")
    sgai_prior = _ratio(pri["sga_expense"], pri["revenue"], "prior revenue")
    lvgi_current = _ratio(
        cur["total_liabilities"], cur["total_assets"], "current total_assets"
    )
    lvgi_prior = _ratio(
        pri["total_liabilities"], pri["total_assets"], "prior total_assets"
    )
    variables = {
        "DSRI": _ratio(dsri_current, dsri_prior, "prior DSRI（prior receivables 为零）"),
        "GMI": _ratio(gmi_prior, gmi_current, "current gross profit"),
        "AQI": _ratio(
            aqi_current_term,
            aqi_prior_term,
            "prior 1-(current_assets+net_ppe)/total_assets",
        ),
        "SGI": _ratio(cur["revenue"], pri["revenue"], "prior revenue"),
        "DEPI": _ratio(depi_prior, depi_current, "current DEPI（current depreciation 为零）"),
        "SGAI": _ratio(sgai_current, sgai_prior, "prior SGAI（prior sga_expense 为零）"),
        "LVGI": _ratio(lvgi_current, lvgi_prior, "prior LVGI（prior total_liabilities 为零）"),
        "TATA": _ratio(
            cur["net_income"] - cur["operating_cash_flow"],
            cur["total_assets"],
            "current total_assets",
        ),
    }
    if zero:
        reason = f"beneish 关键分母为零：{', '.join(zero)}"
        gaps.append(f"beneish：{reason}，组件记 not_applicable。")
        return {"status": "not_applicable", "reason": reason}
    source = _source(section.get("source"), "beneish")
    coefficients = policy["coefficients"]
    m_score = float(policy["intercept"]) + sum(
        float(coefficients[name]) * value for name, value in variables.items()
    )
    threshold = float(policy["m_score_threshold"])
    score = 0.0
    band_reason = ""
    for band in policy["score_bands"]:
        cap = band["max_m_score"]
        if cap is None or m_score <= float(cap):
            score = float(band["score"])
            band_reason = str(band["reason"])
            break
    rounded_variables = {name: _round(value) for name, value in variables.items()}
    evidence = [
        f"{name} = {value}"
        for name, value in rounded_variables.items()
    ]
    evidence.append(
        f"M-Score = {policy['intercept']} + Σ(系数×变量) = {_round(m_score)}；"
        f"阈值 {threshold}；{band_reason}（来源：{source['name']}，as_of：{source['as_of']}）"
    )
    return {
        "status": "computed",
        "score": _round(score),
        "m_score": _round(m_score),
        "threshold": threshold,
        "manipulation_likely": m_score > threshold,
        "variables": rounded_variables,
        "periods": {
            "current": _text(section.get("current_period"), "beneish current_period"),
            "prior": _text(section.get("prior_period"), "beneish prior_period"),
        },
        "evidence": evidence,
    }


def _revenue_recognition(
    section: dict[str, Any] | None, rules: dict[str, Any], gaps: list[str]
) -> dict[str, Any]:
    if section is None:
        gaps.append("revenue_recognition：未提供收入确认红旗指标。")
        return {"status": "missing"}
    metrics = section.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        gaps.append("revenue_recognition：红旗指标为空，组件记 missing。")
        return {"status": "missing", "reason": "no red-flag metrics"}
    source = _source(section.get("source"), "revenue_recognition")
    deduct = 0.0
    evaluated = 0
    flags: list[dict[str, Any]] = []
    evidence: list[str] = []
    for rule in rules["revenue_recognition"]["red_flags"]:
        rule_id = str(rule["id"])
        kind = str(rule["kind"])
        needed = [str(name) for name in rule["metrics"]]
        absent = [name for name in needed if name not in metrics]
        if absent:
            gaps.append(
                f"revenue_recognition：红旗 {rule_id} 缺指标 {', '.join(absent)}，未评估。"
            )
            continue
        values = [_number(metrics[name], f"{rule_id} {name}") for name in needed]
        threshold = float(rule["threshold"])
        if kind == "difference_above":
            value = values[0] - values[1]
            triggered = value > threshold
        elif kind == "above":
            value = values[0]
            triggered = value > threshold
        elif kind == "below":
            value = values[0]
            triggered = value < threshold
        else:
            raise EarningsQualityError(f"unknown red-flag rule kind: {kind}")
        evaluated += 1
        hit = float(rule["deduct"]) if triggered else 0.0
        deduct += hit
        flags.append(
            {
                "id": rule_id,
                "triggered": triggered,
                "value": _round(value),
                "threshold": threshold,
                "deduct": hit,
                "description": str(rule["description"]),
                "source": source,
            }
        )
        state = f"触发，扣分 {hit:g}" if triggered else "未触发"
        evidence.append(
            f"{rule_id}：观测值 {_round(value)}，阈值 {threshold:g}，{state}。"
            f"{rule['description']}（来源：{source['name']}，as_of：{source['as_of']}）"
        )
    if evaluated == 0:
        gaps.append("revenue_recognition：所有红旗指标缺失，组件记 missing。")
        return {"status": "missing", "reason": "no red-flag metrics"}
    return {
        "status": "computed",
        "score": _round(max(0.0, 100.0 - deduct)),
        "red_flags": flags,
        "evidence": evidence,
    }


def _cash_flow(
    section: dict[str, Any] | None, rules: dict[str, Any], gaps: list[str]
) -> dict[str, Any]:
    if section is None:
        gaps.append("cash_flow：未提供年度现金流序列。")
        return {"status": "missing"}
    policy = rules["cash_flow"]
    annual = section.get("annual")
    if not isinstance(annual, list) or len(annual) < int(policy["min_years"]):
        gaps.append(
            f"cash_flow：年度序列不足 {policy['min_years']} 年，组件记 missing。"
        )
        return {"status": "missing", "reason": "insufficient annual series"}
    base_source = _source(section.get("source"), "cash_flow")
    rows: list[dict[str, Any]] = []
    evidence: list[str] = []
    for entry in annual:
        if not isinstance(entry, dict):
            raise EarningsQualityError("cash_flow annual entry must be an object")
        period = _text(entry.get("period"), "cash_flow period")
        net_income = _number(entry.get("net_income"), f"{period} net_income")
        ocf = _number(entry.get("operating_cash_flow"), f"{period} operating_cash_flow")
        fcf = _number(entry.get("free_cash_flow"), f"{period} free_cash_flow")
        source = (
            _source(entry["source"], f"{period} cash_flow")
            if "source" in entry
            else base_source
        )
        ratio = ocf / net_income if net_income > 0 else None
        rows.append(
            {
                "period": period,
                "net_income": net_income,
                "operating_cash_flow": ocf,
                "free_cash_flow": fcf,
                "ocf_ni_ratio": _round(ratio) if ratio is not None else None,
                "fcf_positive": fcf > 0,
                "source": source,
            }
        )
        ratio_text = f"OCF/NI {_round(ratio)}" if ratio is not None else "净利润为负，OCF/NI 不适用"
        evidence.append(
            f"{period}：{ratio_text}，FCF {fcf:g}（{'为正' if fcf > 0 else '非正'}）"
            f"（来源：{source['name']}，as_of：{source['as_of']}）"
        )
    latest = rows[-1]
    ratio = latest["ocf_ni_ratio"]
    band_score = 0.0
    band_reason = ""
    for band in policy["ocf_ni_bands"]:
        floor = band["min_ratio"]
        if ratio is not None and floor is not None and float(ratio) >= float(floor):
            band_score = float(band["score"])
            band_reason = str(band["reason"])
            break
    else:
        lowest = policy["ocf_ni_bands"][-1]
        band_score = float(lowest["score"])
        band_reason = str(lowest["reason"])
    positive_years = sum(1 for row in rows if row["fcf_positive"])
    fcf_share = positive_years / len(rows)
    score = float(policy["ocf_ni_weight"]) * band_score + float(
        policy["fcf_persistence_weight"]
    ) * 100.0 * fcf_share
    evidence.append(
        f"最近年度 OCF/NI 档得分 {band_score:g}（{band_reason}）；"
        f"FCF 为正 {positive_years}/{len(rows)} 年；组件得分 {_round(score)}。"
    )
    return {
        "status": "computed",
        "score": _round(score),
        "ocf_ni_ratio_latest": ratio,
        "fcf_positive_years": positive_years,
        "years": len(rows),
        "annual": rows,
        "evidence": evidence,
    }


def _audit_governance(
    section: dict[str, Any] | None, rules: dict[str, Any], gaps: list[str]
) -> dict[str, Any]:
    if section is None:
        gaps.append("audit_governance：未提供审计与治理信号。")
        return {"status": "missing"}
    policy = rules["audit_governance"]
    source = _source(section.get("source"), "audit_governance")
    opinion = _text(section.get("audit_opinion"), "audit_governance audit_opinion")
    if opinion not in policy["opinion_scores"]:
        raise EarningsQualityError(f"unknown audit opinion: {opinion}")
    score = float(policy["opinion_scores"][opinion])
    signals: list[dict[str, Any]] = [
        {
            "signal": "audit_opinion",
            "severity": str(policy["opinion_severity"][opinion]),
            "detail": opinion,
            "source": source,
        }
    ]
    evidence = [
        f"审计意见 {opinion}，基础分 {score:g}（来源：{source['name']}，as_of：{source['as_of']}）。"
    ]
    deduct = 0.0
    for key, label in (
        ("auditor_change", "审计师变更"),
        ("material_weakness", "内部控制重大缺陷"),
    ):
        flag = section.get(key)
        if not isinstance(flag, bool):
            raise EarningsQualityError(f"audit_governance {key} requires a boolean")
        if flag:
            hit = float(policy["deductions"][key])
            deduct += hit
            signals.append(
                {
                    "signal": key,
                    "severity": str(policy["signal_severity"][key]),
                    "detail": label,
                    "source": source,
                }
            )
            evidence.append(f"{label}，扣分 {hit:g}。")
    red_flags = section.get("governance_red_flags", [])
    if not isinstance(red_flags, list):
        raise EarningsQualityError("governance_red_flags must be a list")
    for flag in red_flags:
        if not isinstance(flag, dict):
            raise EarningsQualityError("governance red flag must be an object")
        flag_id = _text(flag.get("id"), "governance red flag")
        detail = _text(flag.get("description"), f"governance red flag {flag_id}")
        flag_source = _source(flag.get("source"), f"governance red flag {flag_id}")
        hit = float(policy["deductions"]["governance_red_flag_each"])
        deduct += hit
        signals.append(
            {
                "signal": "governance_red_flag",
                "severity": str(policy["signal_severity"]["governance_red_flag"]),
                "detail": f"{flag_id}：{detail}",
                "source": flag_source,
            }
        )
        evidence.append(
            f"治理红旗 {flag_id}：{detail}，扣分 {hit:g}"
            f"（来源：{flag_source['name']}，as_of：{flag_source['as_of']}）。"
        )
    final = max(0.0, score - deduct)
    evidence.append(f"审计与治理组件得分 {_round(final)}。")
    return {
        "status": "computed",
        "score": _round(final),
        "signals": signals,
        "evidence": evidence,
    }


def _assert_no_short_advice(node: object) -> None:
    """Guard: the report must never contain short-selling advice."""
    if isinstance(node, str):
        if SHORT_ADVICE.search(node):
            raise EarningsQualityError("output must not contain short-selling advice")
    elif isinstance(node, dict):
        for key, value in node.items():
            _assert_no_short_advice(key)
            _assert_no_short_advice(value)
    elif isinstance(node, list):
        for value in node:
            _assert_no_short_advice(value)


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    rules = _load_rules()
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise EarningsQualityError("input schema_version must be 1")
    identity = _identity(payload.get("identity"))
    market_scope = _text(payload.get("market_scope"), "market_scope")
    if market_scope not in MARKET_SCOPES:
        raise EarningsQualityError(f"market_scope is not supported: {market_scope}")
    currency = _text(payload.get("currency"), "currency")
    if currency not in CURRENCIES:
        raise EarningsQualityError(f"currency is not supported: {currency}")
    _check_market_contract(
        _load_market_scopes(), market_scope, currency, identity["listing_id"]
    )
    computed_as_of = _as_of(payload.get("computed_as_of"), "computed_as_of")
    computed_moment = _as_of_moment(payload.get("computed_as_of"), "computed_as_of")
    _check_source_times(payload, computed_moment, "input")
    components = payload.get("components")
    if not isinstance(components, dict):
        raise EarningsQualityError("components requires an object")
    gaps: list[str] = []
    periods_summary, baseline_gaps = _accounting_periods(
        payload.get("accounting_periods")
    )
    gaps.extend(baseline_gaps)
    evaluators = {
        "accruals": _accruals,
        "beneish": _beneish,
        "revenue_recognition": _revenue_recognition,
        "cash_flow": _cash_flow,
        "audit_governance": _audit_governance,
    }
    results: dict[str, Any] = {}
    for key in COMPONENT_ORDER:
        results[key] = evaluators[key](
            _component_section(components, key), rules, gaps
        )
    computed = {
        key: result for key, result in results.items() if result["status"] == "computed"
    }
    if not computed:
        raise EarningsQualityError(
            "no earnings-quality component could be computed; refusing to grade"
        )
    weights = rules["component_weights"]
    total_weight = sum(float(weights[key]) for key in computed)
    total = sum(
        float(computed[key]["score"]) * float(weights[key]) for key in computed
    ) / total_weight
    boundaries = rules["grade_boundaries"]
    if total >= float(boundaries["A_min"]):
        grade = "A"
    elif total >= float(boundaries["B_min"]):
        grade = "B"
    elif total >= float(boundaries["C_min"]):
        grade = "C"
    else:
        grade = "D"
    provisional_marks = [
        f"{key}({results[key]['status']})"
        for key in COMPONENT_ORDER
        if results[key]["status"] in {"missing", "not_applicable"}
    ]
    provisional = bool(provisional_marks) or bool(baseline_gaps)
    provisional_parts: list[str] = []
    if provisional_marks:
        provisional_parts.append(
            "组件 "
            + "、".join(provisional_marks)
            + " 缺失或不适用，级别为暂定级，不得视为确定结论；已按已计算组件权重归一化合计。"
        )
    if baseline_gaps:
        provisional_parts.append(
            "会计期间基线不足或缺失（"
            + "；".join(baseline_gaps)
            + "），级别为暂定级，不得视为确定结论。"
        )
    provisional_reason = " ".join(provisional_parts) if provisional_parts else None
    veto = grade in {"C", "D"}
    veto_reason = (
        f"财报质量级别 {grade} 触发 long_entry_veto：交易方案层必须否决多头 entry_plan"
        "（本模块仅多头否决语义，不产生其他方向建议）。"
        if veto
        else None
    )
    report: dict[str, Any] = {
        "identity": identity,
        "schema_version": 1,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "rules_version": rules["rules_version"],
        "computed_as_of": computed_as_of,
        "market_scope": market_scope,
        "currency": currency,
        "accounting_periods": periods_summary,
        "grade": grade,
        "total_score": _round(total),
        "provisional": provisional,
        "provisional_reason": provisional_reason,
        "long_entry_veto": veto,
        "veto_reason": veto_reason,
        "component_weights_applied": {
            key: _round(float(weights[key]) / total_weight) for key in computed
        },
        "components": results,
        "data_gaps": gaps,
    }
    _assert_no_short_advice(report)
    return report


def _write_new(path: Path, content: str) -> None:
    resolved = path.resolve()
    if RUNTIME_ROOT == resolved or RUNTIME_ROOT in resolved.parents:
        raise EarningsQualityError(
            "output path must not be inside the Skill runtime package"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        payload = json.loads(arguments.input.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise EarningsQualityError("input must be a JSON object")
        report = evaluate(payload)
        _write_new(
            arguments.output,
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, json.JSONDecodeError, EarningsQualityError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
