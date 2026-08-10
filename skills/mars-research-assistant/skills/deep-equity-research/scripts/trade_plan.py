#!/usr/bin/env python3
"""Build a long-only conditional trade plan from frozen research artifacts.

Combines a reproducible valuation artifact, a qualified technical-evidence
artifact, an earnings-quality artifact, and a preregistered thesis into a
deterministic, position-agnostic conditional trade plan. All thresholds come
from the versioned preregistered rules file loaded at research start; nothing
is recomputed outside the rules recorded in the output.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
from math import isfinite
from pathlib import Path
import re
import sys
from typing import Any


class TradePlanError(ValueError):
    """Reject incomplete or inconsistent inputs rather than inventing a plan."""


TRADE_DIRECTIVE = re.compile(
    r"买入|卖出|增持|减持|加仓|减仓|建仓|平仓|下单|持仓比例|做空|沽空|卖空|"
    r"\bbuy\b|\bsell\b|\bshort\b|\bposition size\b|\bplace (?:an )?order\b",
    re.IGNORECASE,
)
MARKET_SCOPES = {"us", "hk", "a_share", "ah_compare"}
CURRENCIES = {"USD", "HKD", "CNY"}
EVIDENCE_TIMEFRAMES = {"1D", "1d"}
EVIDENCE_STATUSES = {"qualified", "rejected"}
EVIDENCE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
EARNINGS_GRADES = {"A", "B", "C", "D"}
SCRIPT_PATH = Path(__file__).resolve()
RUNTIME_ROOT = SCRIPT_PATH.parents[3]
RULES_PATH = SCRIPT_PATH.parents[1] / "reference" / "preregistered_rules.json"
MARKET_CONTRACTS_PATH = SCRIPT_PATH.parents[1] / "reference" / "market_contracts.json"
ENGINE = "skills/deep-equity-research/scripts/trade_plan.py"
ENGINE_VERSION = "1.0.0"
ENTRY_BASIS = "value_zone ∩ technical_support ± ATR"
INVALIDATION_RULE = "support - 2×ATR14"
A_SHARE_SUFFIXES = (".SS", ".SH", ".SZ")
HK_SUFFIX = ".HK"
AH_COMPARE_FX_PAIR = "CNY/HKD"
VIE_ADR_FIELDS = (
    "adr_conversion_ratio",
    "listing_regulator",
    "delisting_or_conversion_risk",
    "vie_contract_control_risk",
)
MISSING_FIELD = "未获取到"


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TradePlanError(f"{context} requires text")
    return value.strip()


def _is_version_one(value: object) -> bool:
    return type(value) is int and value == 1


def _conditional_text(value: object, context: str) -> str:
    statement = _text(value, context)
    if TRADE_DIRECTIVE.search(statement):
        raise TradePlanError(f"{context} contains a trade directive")
    return statement


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TradePlanError(f"{context} requires a finite number")
    result = float(value)
    if not isfinite(result):
        raise TradePlanError(f"{context} requires a finite number")
    return result


def _rounded(value: float) -> float:
    return round(value, 6)


def _as_of_moment(value: object, context: str) -> tuple[str, datetime]:
    text = _text(value, context)
    if "T" not in text:
        raise TradePlanError(
            f"{context} requires a complete timestamp with timezone"
        )
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as error:
        raise TradePlanError(f"{context} requires an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TradePlanError(f"{context} timestamp requires a timezone")
    return text, parsed.astimezone(timezone.utc)


def _check_source_times(node: object, computed: datetime, context: str) -> None:
    """Reject a tampered upstream artifact that records future source data."""
    if isinstance(node, dict):
        if {"name", "kind", "as_of", "url"}.issubset(node):
            _, source_moment = _as_of_moment(node["as_of"], f"{context} source")
            if source_moment > computed:
                raise TradePlanError(
                    f"{context} source as_of is after computed_as_of"
                )
        for key, value in node.items():
            _check_source_times(value, computed, f"{context}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_source_times(value, computed, f"{context}[{index}]")


def _identity(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TradePlanError(f"{context} identity requires an object")
    record: dict[str, Any] = {
        field: _text(value.get(field), f"{context} identity {field}")
        for field in ("issuer_id", "listing_id", "case_id")
    }
    for field in ("artifact_version", "schema_version"):
        number = value.get(field)
        if not _is_version_one(number):
            raise TradePlanError(f"{context} identity {field} must be 1")
        record[field] = number
    return record


def _top_level_schema_version(payload: dict[str, Any], input_name: str) -> None:
    version = payload.get("schema_version")
    if not _is_version_one(version):
        raise TradePlanError(f"{input_name} input schema_version must be 1")


def _load_json(path: Path, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TradePlanError(f"{context} is not a readable JSON object: {error}") from error
    if not isinstance(payload, dict):
        raise TradePlanError(f"{context} must be a JSON object")
    return payload


def _load_rules() -> dict[str, Any]:
    rules = _load_json(RULES_PATH, "preregistered rules")
    return {
        "rules_version": _text(rules.get("rules_version"), "preregistered rules"),
        "horizon_min": int(
            _number(
                rules.get("horizon_months", {}).get("default_min")
                if isinstance(rules.get("horizon_months"), dict)
                else None,
                "rules horizon_months.default_min",
            )
        ),
        "horizon_max": int(
            _number(
                rules.get("horizon_months", {}).get("default_max")
                if isinstance(rules.get("horizon_months"), dict)
                else None,
                "rules horizon_months.default_max",
            )
        ),
        "safety_margin": _number(rules.get("safety_margin"), "rules safety_margin"),
        "entry_atr_tolerance": _number(
            rules.get("entry_atr_tolerance"), "rules entry_atr_tolerance"
        ),
        "invalidation_atr_multiple": _number(
            rules.get("invalidation_atr_multiple"), "rules invalidation_atr_multiple"
        ),
        "technical_evidence_max_age_days": _number(
            rules.get("technical_evidence_max_age_days"),
            "rules technical_evidence_max_age_days",
        ),
        "min_reward_risk_ratio": _number(
            rules.get("min_reward_risk_ratio"), "rules min_reward_risk_ratio"
        ),
    }


def _load_market_scopes() -> dict[str, Any]:
    contracts = _load_json(MARKET_CONTRACTS_PATH, "market contracts")
    scopes = contracts.get("scopes")
    if not isinstance(scopes, dict):
        raise TradePlanError("market contracts scopes must be an object")
    return scopes


def _validate_scope_currency_listing(
    scopes: dict[str, Any], market_scope: str, currency: str, listing_id: str
) -> None:
    if market_scope == "ah_compare":
        allowed_currencies = {
            scopes["a_share"]["currency"], scopes["hk"]["currency"]
        }
        declared_suffixes = tuple(
            suffix
            for scope in ("a_share", "hk")
            for suffix in scopes[scope].get("suffixes", [])
            if suffix
        )
        allows_bare = False
    else:
        contract = scopes.get(market_scope)
        if not isinstance(contract, dict):
            raise TradePlanError(f"market_scope is not supported: {market_scope}")
        allowed_currencies = {contract.get("currency")}
        declared_suffixes = tuple(
            suffix for suffix in contract.get("suffixes", []) if suffix
        )
        allows_bare = "" in contract.get("suffixes", [])
    if currency not in allowed_currencies:
        raise TradePlanError(
            f"market_scope {market_scope} does not allow currency {currency}"
        )
    if declared_suffixes and listing_id.endswith(declared_suffixes):
        return
    if allows_bare:
        foreign_suffixes = tuple(
            suffix
            for name, contract in scopes.items()
            if name != market_scope and isinstance(contract, dict)
            for suffix in contract.get("suffixes", [])
            if suffix
        )
        if not any(listing_id.endswith(suffix) for suffix in foreign_suffixes):
            return
    raise TradePlanError(
        f"listing_id {listing_id} suffix does not match market_scope {market_scope}"
    )


TERMINAL_CHECK_NAMES = (
    "long_run_growth",
    "mature_margin",
    "reinvestment_roic_consistency",
)
TERMINAL_CHECK_STATUSES = {"pass", "warn", "fail"}


def _terminal_failures(valuation: dict[str, Any]) -> list[tuple[str, str]]:
    results = valuation.get("results")
    failures: list[tuple[str, str]] = []
    for model in ("dcf", "driver_dcf"):
        entry = results.get(model) if isinstance(results, dict) else None
        if not isinstance(entry, dict) or entry.get("status") != "computed":
            continue
        checks = entry.get("terminal_value_checks")
        if not isinstance(checks, dict):
            failures.append((f"{model}.terminal_checks_missing", "未提供完整终值三查结果。"))
            continue
        quality = entry.get("quality") if model == "driver_dcf" else None
        quality_usable = isinstance(quality, dict) and quality.get("status") == "usable"
        for name in TERMINAL_CHECK_NAMES:
            check = checks.get(name)
            if not isinstance(check, dict):
                failures.append((f"{model}.{name}_missing", "终值三查缺少该检查结果。"))
                continue
            status = check.get("status")
            if status not in TERMINAL_CHECK_STATUSES:
                failures.append((f"{model}.{name}", f"终值检查状态非法：{status!r}。"))
                continue
            if status == "fail" or (model == "driver_dcf" and quality_usable and status != "pass"):
                detail = check.get("detail")
                failures.append(
                    (
                        f"{model}.{name}",
                        detail if isinstance(detail, str) else "终值检查未通过或缺少失败详情",
                    )
                )
    return failures


def _check_identities(identities: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Require one issuer and one case across inputs."""
    case_ids = {name: record["case_id"] for name, record in identities.items()}
    if len(set(case_ids.values())) != 1:
        detail = ", ".join(f"{name}={case_id}" for name, case_id in case_ids.items())
        raise TradePlanError(f"input identity case_id mismatch: {detail}")
    issuer_ids = {name: record["issuer_id"] for name, record in identities.items()}
    if len(set(issuer_ids.values())) != 1:
        detail = ", ".join(
            f"{name}={issuer_id}" for name, issuer_id in issuer_ids.items()
        )
        raise TradePlanError(f"input identity issuer_id mismatch: {detail}")
    return next(iter(case_ids.values())), next(iter(issuer_ids.values()))


def _check_listings(
    identities: dict[str, dict[str, Any]], market_scope: str
) -> None:
    """All inputs share one listing_id; the sole exception is an ah_compare
    case carrying exactly one A-share listing (.SS/.SH/.SZ) and one HK
    listing (.HK), per reference/market_contracts.json."""
    listings = {name: record["listing_id"] for name, record in identities.items()}
    unique = set(listings.values())
    if len(unique) == 1:
        return
    if market_scope == "ah_compare" and len(unique) == 2:
        a_share = [item for item in unique if item.endswith(A_SHARE_SUFFIXES)]
        hk = [item for item in unique if item.endswith(HK_SUFFIX)]
        if len(a_share) == 1 and len(hk) == 1:
            return
    detail = ", ".join(f"{name}={listing}" for name, listing in listings.items())
    raise TradePlanError(f"input identity listing_id mismatch: {detail}")


def _ah_compare(
    valuation: dict[str, Any],
    identities: dict[str, dict[str, Any]],
    market_scope: str,
) -> dict[str, Any] | None:
    """Enforce the ah_compare contract fail closed: a uniquely matched A/H
    pair, CNY/HKD fx handling, and the five mandatory disclosures."""
    if market_scope != "ah_compare":
        return None
    block = valuation.get("ah_compare")
    if not isinstance(block, dict):
        raise TradePlanError(
            "ah_compare 市场范围要求估值工件携带 ah_compare 配对与披露块，缺失即停止。"
        )
    pair = block.get("pair")
    if not isinstance(pair, dict):
        raise TradePlanError("ah_compare pair requires an object")
    a_share_listing = _text(pair.get("a_share_listing_id"), "ah_compare pair")
    hk_listing = _text(pair.get("hk_listing_id"), "ah_compare pair")
    listings = {record["listing_id"] for record in identities.values()}
    if (
        not a_share_listing.endswith(A_SHARE_SUFFIXES)
        or not hk_listing.endswith(HK_SUFFIX)
        or listings != {a_share_listing, hk_listing}
    ):
        raise TradePlanError(
            "ah_compare 无法唯一配对：输入 listing 与声明的 A/H 对不一致，"
            "已按 pair_failure 停止，请澄清配对后重试。"
        )
    fx_pair = _text(block.get("fx_pair"), "ah_compare fx_pair")
    if fx_pair != AH_COMPARE_FX_PAIR:
        raise TradePlanError(f"ah_compare fx_pair must be {AH_COMPARE_FX_PAIR}")
    fx_rate = _number(block.get("fx_rate"), "ah_compare fx_rate")
    if fx_rate <= 0:
        raise TradePlanError("ah_compare fx_rate must be positive")
    share_right_ratio = _number(
        block.get("share_right_ratio"), "ah_compare share_right_ratio"
    )
    if share_right_ratio <= 0:
        raise TradePlanError("ah_compare share_right_ratio must be positive")
    return {
        "pair": {
            "a_share_listing_id": a_share_listing,
            "hk_listing_id": hk_listing,
        },
        "fx_pair": fx_pair,
        "fx_rate": fx_rate,
        "share_right_ratio": share_right_ratio,
        "liquidity_diff": _text(
            block.get("liquidity_diff"), "ah_compare liquidity_diff"
        ),
        "trading_day_diff": _text(
            block.get("trading_day_diff"), "ah_compare trading_day_diff"
        ),
        "premium_discount": _number(
            block.get("premium_discount"), "ah_compare premium_discount"
        ),
    }


def _vie_adr(valuation: dict[str, Any]) -> dict[str, Any] | None:
    """Record VIE/ADR identifications for US-listed Chinese issuers. Missing
    data is recorded as 未获取到 and never triggers an automatic discount."""
    block = valuation.get("vie_adr")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise TradePlanError("vie_adr requires an object")
    flag = block.get("us_listed_chinese_issuer")
    if not isinstance(flag, bool):
        raise TradePlanError("vie_adr us_listed_chinese_issuer requires a boolean")
    if not flag:
        return {"us_listed_chinese_issuer": False}
    record: dict[str, Any] = {"us_listed_chinese_issuer": True}
    for field in VIE_ADR_FIELDS:
        value = block.get(field)
        if value is None:
            record[field] = MISSING_FIELD
        elif isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise TradePlanError(f"vie_adr {field} requires a number or text")
        elif isinstance(value, str):
            record[field] = _text(value, f"vie_adr {field}")
        else:
            record[field] = _number(value, f"vie_adr {field}")
    return record


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _resolve_value_source(
    valuation: dict[str, Any], safety_margin: float
) -> dict[str, Any] | None:
    """Resolve the value band deterministically: a driver-based DCF whose
    generic quality gate is usable takes precedence. An explicitly supplied,
    usable P/E model is the next alternative; a driver DCF or P/E model that
    fails its quality gate blocks fallback to the legacy baseline DCF. Without
    either explicit quality-gated model the legacy behavior is unchanged: a
    computed DCF value zone first, otherwise the first computed point estimate
    among EPV, EVA, SOTP anchored into an explicit band. Returns None when no
    model applies."""
    results = valuation.get("results")
    if not isinstance(results, dict):
        return None
    driver = results.get("driver_dcf")
    if isinstance(driver, dict):
        quality = driver.get("quality")
        quality_status = quality.get("status") if isinstance(quality, dict) else None
        if driver.get("status") == "computed" and quality_status == "usable":
            zone = driver.get("value_zone")
            if isinstance(zone, dict):
                low = _number_or_none(zone.get("low"))
                high = _number_or_none(zone.get("high"))
                weighted = _number_or_none(driver.get("probability_weighted_per_share"))
                if (
                    low is not None
                    and high is not None
                    and 0 < low <= high
                    and weighted is not None
                    and weighted > 0
                ):
                    return {
                        "model": "driver_dcf",
                        "zone_low": low,
                        "zone_high": high,
                        "target": weighted,
                        "target_basis": "driver_dcf",
                        "entry_basis": ENTRY_BASIS,
                        "band_note": None,
                    }
    pe = results.get("pe")
    if isinstance(pe, dict):
        quality = pe.get("quality")
        quality_status = quality.get("status") if isinstance(quality, dict) else None
        if pe.get("status") == "computed" and quality_status == "usable":
            zone = pe.get("value_zone")
            low = _number_or_none(zone.get("low")) if isinstance(zone, dict) else None
            high = _number_or_none(zone.get("high")) if isinstance(zone, dict) else None
            weighted = _number_or_none(pe.get("probability_weighted_per_share"))
            if (
                low is not None
                and high is not None
                and 0 < low <= high
                and weighted is not None
                and weighted > 0
            ):
                return {
                    "model": "pe",
                    "zone_low": low,
                    "zone_high": high,
                    "target": weighted,
                    "target_basis": "pe",
                    "entry_basis": ENTRY_BASIS,
                    "band_note": None,
                }
        return None
    if isinstance(driver, dict):
        return None
    dcf = results.get("dcf")
    if isinstance(dcf, dict) and dcf.get("status") == "computed":
        zone = dcf.get("value_zone")
        if isinstance(zone, dict):
            low = _number_or_none(zone.get("low"))
            high = _number_or_none(zone.get("high"))
            weighted = _number_or_none(dcf.get("probability_weighted_per_share"))
            if (
                low is not None
                and high is not None
                and 0 < low <= high
                and weighted is not None
                and weighted > 0
            ):
                return {
                    "model": "dcf",
                    "zone_low": low,
                    "zone_high": high,
                    "target": weighted,
                    "target_basis": "probability_weighted",
                    "entry_basis": ENTRY_BASIS,
                    "band_note": None,
                }
    for model, key in (
        ("epv", "epv_per_share"),
        ("eva", "residual_income_per_share"),
        ("sotp", "per_share"),
    ):
        entry = results.get(model)
        if not isinstance(entry, dict) or entry.get("status") != "computed":
            continue
        anchor = _number_or_none(entry.get(key))
        if anchor is None or anchor <= 0:
            continue
        return {
            "model": model,
            "zone_low": anchor * (1 - safety_margin),
            "zone_high": anchor * (1 + safety_margin),
            "target": anchor,
            "target_basis": model,
            "entry_basis": f"{model} 明示价值带 ∩ technical_support ± ATR",
            "band_note": (
                "估值 DCF 未给出有效价值区间，价值带以 "
                f"{model} 点估值为锚按 ±safety_margin 构成明示区间。"
            ),
        }
    return None


def _nearest_level(
    key_levels: list[dict[str, Any]], side: str, close: float
) -> dict[str, Any] | None:
    levels = [level for level in key_levels if level.get("side") == side]
    if not levels:
        return None
    return min(levels, key=lambda level: abs(float(level["price"]) - close))


def _key_levels(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TradePlanError("technical evidence key_levels must be a list")
    levels: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise TradePlanError("technical evidence key level must be an object")
        side = _text(item.get("side"), "technical evidence key level")
        if side not in {"support", "resistance"}:
            raise TradePlanError("technical evidence key level side is invalid")
        levels.append({"side": side, "price": _number(item.get("price"), "key level")})
    return levels


def _thesis(payload: dict[str, Any]) -> dict[str, Any]:
    view = payload.get("independent_view")
    if not isinstance(view, dict):
        raise TradePlanError("thesis independent_view requires an object")
    present = view.get("present")
    if not isinstance(present, bool):
        raise TradePlanError("thesis independent_view.present requires a boolean")
    statement = view.get("statement")
    view_statement = (
        _conditional_text(statement, "thesis independent_view.statement")
        if present
        else (statement if isinstance(statement, str) else None)
    )
    hypothesis = _conditional_text(
        payload.get("preregistered_hypothesis"), "thesis preregistered_hypothesis"
    )
    falsification = payload.get("falsification_conditions")
    if not isinstance(falsification, list):
        raise TradePlanError("thesis falsification_conditions must be a list")
    falsification_conditions = [
        _conditional_text(item, "thesis falsification condition")
        for item in falsification
    ]
    counter_thesis = _conditional_text(
        payload.get("counter_thesis"), "thesis counter_thesis"
    )
    premortem = _conditional_text(payload.get("premortem"), "thesis premortem")
    base_rate = payload.get("base_rate")
    if not isinstance(base_rate, dict):
        raise TradePlanError("thesis base_rate requires an object")
    percentile = base_rate.get("percentile")
    if percentile is not None:
        percentile = _number(percentile, "thesis base_rate.percentile")
        if not 0 <= percentile <= 1:
            raise TradePlanError("thesis base_rate.percentile must be within [0, 1]")
    exceed_reason = base_rate.get("exceed_reason")
    if exceed_reason is not None:
        exceed_reason = _conditional_text(exceed_reason, "thesis base_rate.exceed_reason")
    verification_metrics = base_rate.get("verification_metrics")
    if verification_metrics is not None:
        if not isinstance(verification_metrics, list):
            raise TradePlanError("thesis base_rate.verification_metrics must be a list")
        verification_metrics = [
            _conditional_text(item, "thesis base_rate verification metric")
            for item in verification_metrics
        ]
    cash_question = payload.get("cash_question")
    if not isinstance(cash_question, dict):
        raise TradePlanError("thesis cash_question requires an object")
    would_deploy = cash_question.get("would_deploy")
    if not isinstance(would_deploy, bool):
        raise TradePlanError("thesis cash_question.would_deploy requires a boolean")
    cash_reason = _conditional_text(
        cash_question.get("reason"), "thesis cash_question.reason"
    )
    return {
        "view_present": present,
        "view_statement": view_statement,
        "hypothesis": hypothesis,
        "falsification_conditions": falsification_conditions,
        "counter_thesis": counter_thesis,
        "premortem": premortem,
        "base_rate_percentile": percentile,
        "base_rate_exceed_reason": exceed_reason,
        "base_rate_verification_metrics": verification_metrics,
        "would_deploy": would_deploy,
        "cash_reason": cash_reason,
    }


def build_plan(
    valuation: dict[str, Any],
    evidence: dict[str, Any],
    earnings_quality: dict[str, Any],
    thesis_payload: dict[str, Any],
    rules: dict[str, Any],
    now: datetime | None,
) -> dict[str, Any]:
    for input_name, payload in (
        ("valuation", valuation),
        ("earnings-quality", earnings_quality),
        ("thesis", thesis_payload),
        ("technical evidence", evidence),
    ):
        _top_level_schema_version(payload, input_name)
    valuation_identity = _identity(valuation.get("identity"), "valuation")
    earnings_identity = _identity(earnings_quality.get("identity"), "earnings-quality")
    thesis_identity = _identity(thesis_payload.get("identity"), "thesis")
    # 技术证据必须携带完整身份（含 artifact_version/schema_version），无可选旁路。
    evidence_identity = _identity(evidence.get("identity"), "technical evidence")
    case_id, issuer_id = _check_identities(
        {
            "valuation": valuation_identity,
            "earnings_quality": earnings_identity,
            "thesis": thesis_identity,
        }
    )
    if (
        evidence_identity["issuer_id"] != issuer_id
        or evidence_identity["case_id"] != case_id
    ):
        raise TradePlanError(
            "technical evidence identity mismatch: "
            f"issuer_id={evidence_identity['issuer_id']} "
            f"case_id={evidence_identity['case_id']}"
        )
    market_scope = _text(valuation.get("market_scope"), "valuation market_scope")
    if market_scope not in MARKET_SCOPES:
        raise TradePlanError(
            f"valuation market_scope is not supported: {market_scope}"
        )
    currency = _text(valuation.get("currency"), "valuation currency")
    if currency not in CURRENCIES:
        raise TradePlanError(f"valuation currency is not supported: {currency}")
    _validate_scope_currency_listing(
        _load_market_scopes(), market_scope, currency, valuation_identity["listing_id"]
    )
    identities = {
        "valuation": valuation_identity,
        "earnings_quality": earnings_identity,
        "thesis": thesis_identity,
        "technical_evidence": evidence_identity,
    }
    _check_listings(identities, market_scope)
    ah_compare = _ah_compare(valuation, identities, market_scope)
    vie_adr = _vie_adr(valuation)

    valuation_as_of_text, valuation_as_of = _as_of_moment(
        valuation.get("computed_as_of"), "valuation"
    )
    del valuation_as_of_text
    _check_source_times(valuation, valuation_as_of, "valuation")
    reference_now = now if now is not None else valuation_as_of

    _, earnings_as_of = _as_of_moment(
        earnings_quality.get("computed_as_of"), "earnings-quality"
    )
    _check_source_times(earnings_quality, earnings_as_of, "earnings-quality")
    _check_source_times(thesis_payload, valuation_as_of, "thesis")
    if earnings_as_of > reference_now:
        raise TradePlanError(
            "earnings-quality computed_as_of must not be after the valuation reference time"
        )

    evidence_status = _text(evidence.get("status"), "technical evidence status")
    if evidence_status not in EVIDENCE_STATUSES:
        raise TradePlanError(
            f"technical evidence status is not a known status: {evidence_status}"
        )
    evidence_timeframe = _text(
        evidence.get("timeframe"), "technical evidence timeframe"
    )
    if evidence_timeframe not in EVIDENCE_TIMEFRAMES:
        raise TradePlanError(
            "technical evidence timeframe must be a daily (1D) timeframe: "
            f"{evidence_timeframe}"
        )
    evidence_symbol = _text(evidence.get("symbol"), "technical evidence symbol")
    bare_us_alias = (
        "." not in evidence_symbol
        and evidence_identity["listing_id"] == f"{evidence_symbol}.US"
    )
    if evidence_symbol != evidence_identity["listing_id"] and not bare_us_alias:
        raise TradePlanError(
            "technical evidence symbol does not match its identity listing_id: "
            f"{evidence_symbol} != {evidence_identity['listing_id']}"
        )
    evidence_id = _text(evidence.get("evidence_id"), "technical evidence")
    if not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
        raise TradePlanError(
            "technical evidence evidence_id must match sha256:<64 lowercase hex>: "
            f"{evidence_id}"
        )
    evidence_as_of_text, evidence_as_of = _as_of_moment(
        evidence.get("as_of"), "technical evidence"
    )
    if evidence_as_of > valuation_as_of:
        raise TradePlanError(
            "technical evidence as_of must not be after valuation computed_as_of: "
            f"{evidence_as_of_text} > {valuation.get('computed_as_of')}"
        )
    indicators = evidence.get("indicators")
    latest = indicators.get("latest") if isinstance(indicators, dict) else None
    if not isinstance(latest, dict):
        raise TradePlanError("technical evidence indicators.latest requires an object")
    close = _number(latest.get("close"), "technical evidence latest close")
    atr14 = _number(latest.get("atr14"), "technical evidence latest atr14")
    if close <= 0 or atr14 <= 0:
        raise TradePlanError("technical evidence close and atr14 must be positive")
    key_levels = _key_levels(evidence.get("key_levels"))
    for level in key_levels:
        price = level["price"]
        if price <= 0:
            raise TradePlanError(
                "technical evidence key level price must be positive"
            )
        if level["side"] == "support" and price > close:
            raise TradePlanError(
                "technical evidence support must not be above the latest close"
            )
        if level["side"] == "resistance" and price < close:
            raise TradePlanError(
                "technical evidence resistance must not be below the latest close"
            )
    support = _nearest_level(key_levels, "support", close)
    resistance = _nearest_level(key_levels, "resistance", close)

    value_source = _resolve_value_source(valuation, rules["safety_margin"])
    terminal_failures = _terminal_failures(valuation)

    grade = _text(earnings_quality.get("grade"), "earnings-quality grade")
    if grade not in EARNINGS_GRADES:
        raise TradePlanError("earnings-quality grade must be A, B, C, or D")
    long_entry_veto = earnings_quality.get("long_entry_veto")
    if not isinstance(long_entry_veto, bool):
        raise TradePlanError("earnings-quality long_entry_veto requires a boolean")
    if long_entry_veto != (grade in {"C", "D"}):
        raise TradePlanError(
            "earnings-quality long_entry_veto contradicts grade: "
            f"grade={grade} but long_entry_veto={long_entry_veto}"
        )
    veto_reason = earnings_quality.get("veto_reason")

    thesis = _thesis(thesis_payload)

    gates: dict[str, dict[str, Any]] = {}

    def record(gate: str, passed: bool, reason: str) -> bool:
        gates[gate] = {"pass": passed, "reason": reason}
        return passed

    # Gate 1: independent view and the cash question.
    if not thesis["view_present"] or not thesis["view_statement"]:
        record("independent_view", False, "缺少独立观点陈述，不产生方案。")
    elif not thesis["would_deploy"]:
        record(
            "independent_view",
            False,
            f"现金问题回答为不部署：{thesis['cash_reason']}",
        )
    else:
        record("independent_view", True, "独立观点与现金问题均已明确回答。")

    # Gate 2: decision-discipline data quality of the thesis itself.
    missing = []
    if not thesis["falsification_conditions"]:
        missing.append("可证伪条件")
    if not thesis["counter_thesis"]:
        missing.append("反方论证")
    if not thesis["premortem"]:
        missing.append("事前风险预演")
    if thesis["base_rate_percentile"] is None:
        missing.append("基准率分位")
    if not thesis["base_rate_exceed_reason"]:
        missing.append("超越基准率理由")
    if not thesis["base_rate_verification_metrics"]:
        missing.append("基准率验证指标")
    if missing:
        record("data_quality", False, f"决策纪律材料缺失：{'、'.join(missing)}。")
    else:
        record(
            "data_quality",
            True,
            "可证伪条件、反方论证、事前预演与基准率材料齐全。",
        )

    # Gate 3: valuation must yield a usable value band from an applicable model.
    if terminal_failures:
        detail = "；".join(f"{name}：{reason}" for name, reason in terminal_failures)
        record(
            "valuation",
            False,
            f"估值 DCF 终值检查未通过：{detail}。估值门不通过。",
        )
    elif value_source is None:
        record(
            "valuation",
            False,
            "估值无任何已计算且通过质量门槛的适用模型（driver_dcf/pe/dcf/epv/eva/sotp）给出有效价值，"
            "无法构成价值带。",
        )
    elif value_source["model"] == "dcf":
        record("valuation", True, "三情景 DCF 已计算，价值区间有效。")
    elif value_source["model"] == "driver_dcf":
        record(
            "valuation",
            True,
            "驱动型 DCF 质量门槛为 usable，价值区间有效。",
        )
    elif value_source["model"] == "pe":
        record(
            "valuation",
            True,
            "PE 模型使用显式前瞻/正常化 EPS 与情景倍数，质量门槛为 usable，价值区间有效。",
        )
    else:
        record(
            "valuation",
            True,
            f"估值采用已计算的 {value_source['model']} 点估值构成明示价值区间。",
        )

    # Gate 4: earnings quality veto.
    if long_entry_veto:
        detail = (
            f"：{veto_reason.rstrip('。')}"
            if isinstance(veto_reason, str) and veto_reason
            else ""
        )
        record(
            "earnings_quality",
            False,
            f"财报质量等级 {grade} 触发多头入场否决{detail}。",
        )
    else:
        record("earnings_quality", True, f"财报质量等级 {grade}，未触发否决。")

    # Gate 5: technical evidence qualification and freshness.
    max_age = timedelta(days=rules["technical_evidence_max_age_days"])
    technical_valid_until = evidence_as_of + max_age
    if evidence_status != "qualified":
        record(
            "technical_evidence",
            False,
            f"技术证据状态为 {evidence_status}，未通过质量门。",
        )
    elif evidence_as_of < reference_now - max_age:
        record(
            "technical_evidence",
            False,
            f"技术证据 as_of {evidence_as_of_text} 早于有效期下限，已过期。",
        )
    elif resistance is None:
        record(
            "technical_evidence",
            False,
            "技术证据缺少阻力位，无法标注技术目标，质量门不通过。",
        )
    else:
        record("technical_evidence", True, "技术证据合格且在有效期内。")

    upstream_ok = all(
        gates[name]["pass"]
        for name in (
            "independent_view",
            "data_quality",
            "valuation",
            "earnings_quality",
            "technical_evidence",
        )
    )

    # Price-zone computation (only meaningful when upstream gates pass).
    margin = 1 - rules["safety_margin"]
    value_band = (
        {
            "low": _rounded(value_source["zone_low"] * margin),
            "high": _rounded(value_source["zone_high"] * margin),
        }
        if value_source is not None
        else None
    )
    entry_basis = (
        value_source["entry_basis"] if value_source is not None else ENTRY_BASIS
    )
    tolerance = rules["entry_atr_tolerance"] * atr14
    invalidation_multiple = rules["invalidation_atr_multiple"]

    entry_zone: dict[str, float] | None = None
    support_zone: dict[str, float] | None = None
    invalidation_level: float | None = None
    reward_risk: float | None = None
    downside_pct: float | None = None

    if not upstream_ok:
        failed = next(name for name in gates if not gates[name]["pass"])
        record(
            "value_technical_intersection",
            False,
            f"前置决策门 {failed} 未通过，不计算价值/技术交集。",
        )
        record("reward_risk", False, "前置决策门未通过，不计算收益风险比。")
    else:
        if support is None:
            record(
                "value_technical_intersection",
                False,
                "技术证据缺少支撑位，无法构成技术支持区。",
            )
        else:
            support_zone = {
                "low": _rounded(float(support["price"]) - tolerance),
                "high": _rounded(float(support["price"]) + tolerance),
            }
            low = max(value_band["low"], support_zone["low"])
            high = min(value_band["high"], support_zone["high"])
            if low > high:
                record(
                    "value_technical_intersection",
                    False,
                    "价值带与技术支持区无交集，不产生入场区间。",
                )
            else:
                entry_zone = {"low": _rounded(low), "high": _rounded(high)}
                record(
                    "value_technical_intersection",
                    True,
                    "价值带与技术支持区存在可解释交集。",
                )
        if entry_zone is None or support is None:
            record("reward_risk", False, "无入场区间，不计算收益风险比。")
        else:
            invalidation_level = _rounded(
                float(support["price"]) - invalidation_multiple * atr14
            )
            risk = entry_zone["low"] - invalidation_level
            if risk <= 0:
                record(
                    "reward_risk",
                    False,
                    "入场区间下沿不高于技术失效位，收益风险比无效。",
                )
            else:
                reward_risk = _rounded(
                    (float(value_source["target"]) - entry_zone["high"]) / risk
                )
                downside_pct = _rounded(risk / entry_zone["low"])
                if reward_risk < rules["min_reward_risk_ratio"]:
                    record(
                        "reward_risk",
                        False,
                        f"收益风险比 {reward_risk} 低于预注册下限 "
                        f"{rules['min_reward_risk_ratio']}。",
                    )
                    reward_risk = None
                else:
                    record(
                        "reward_risk",
                        True,
                        f"收益风险比 {reward_risk} 达到预注册下限。",
                    )

    all_pass = all(gate["pass"] for gate in gates.values())
    status = "entry_plan" if all_pass else "watch"

    watch_conditions: dict[str, list[str]] = {
        "independent_view": ["形成并记录独立观点且现金问题回答为肯定后重估。"],
        "data_quality": ["补齐反方论证、事前预演与基准率材料后重估。"],
        "valuation": ["完成任一适用估值模型（driver_dcf/pe/dcf/epv/eva/sotp）的可复算计算并通过质量门槛后重估。"],
        "earnings_quality": ["财报质量等级回到 B 及以上（未触发否决）后重估。"],
        "technical_evidence": ["更新技术证据（质量门合格且 as_of 在有效期内）后重估。"],
        "value_technical_intersection": [
            "等待价格回到价值带与技术支持区的交集，或价值区间上修后重估。"
        ],
        "reward_risk": ["等待基本面目标与失效位之间的距离改善至收益风险比达标后重估。"],
    }
    trigger_conditions = [
        f"价格进入入场区间且技术证据不晚于 {technical_valid_until.isoformat()}。",
        "入场前复核财报质量等级仍未触发否决。",
        "预注册命题未被证伪条件触发。",
    ]
    what_would_change = [
        condition
        for name, gate in gates.items()
        if not gate["pass"]
        for condition in watch_conditions[name]
    ]

    null_reason = gates and next(
        (gates[name]["reason"] for name in gates if not gates[name]["pass"]),
        "决策门未全部通过。",
    )

    if status == "entry_plan":
        entry_plan = {
            "zone": entry_zone,
            "basis": entry_basis,
            "value_band": value_band,
            "technical_support_zone": support_zone,
            "trigger_conditions": trigger_conditions,
        }
        target_plan = {
            "technical_target": {
                "level": _rounded(float(resistance["price"])) if resistance else None,
                "source_level": resistance,
            },
            "fundamental_target": {
                "level": _rounded(float(value_source["target"])),
                "basis": value_source["target_basis"],
            },
        }
        invalidation_plan = {
            "technical_invalidation": {
                "level": invalidation_level,
                "rule": INVALIDATION_RULE,
                "reference_support": _rounded(float(support["price"])),
                "atr14": _rounded(atr14),
            },
            "thesis_invalidation": thesis["falsification_conditions"],
        }
        veto = None
    else:
        entry_plan = {
            "zone": None,
            "basis": entry_basis,
            "reason": null_reason,
            "trigger_conditions": trigger_conditions,
            "what_would_change": what_would_change,
        }
        target_plan = {
            "technical_target": {"level": None, "reason": null_reason},
            "fundamental_target": {
                "level": None,
                "basis": (
                    value_source["target_basis"]
                    if value_source is not None
                    else "unavailable"
                ),
                "reason": null_reason,
            },
        }
        invalidation_plan = {
            "technical_invalidation": {
                "level": None,
                "rule": INVALIDATION_RULE,
                "reason": null_reason,
            },
            "thesis_invalidation": thesis["falsification_conditions"],
        }
        failed_gate = next(name for name in gates if not gates[name]["pass"])
        veto = {"gate": failed_gate, "reason": gates[failed_gate]["reason"]}
        downside_pct = None
        reward_risk = None

    data_gaps: list[str] = []
    if value_source is None:
        data_gaps.append(
            "估值缺少任何已计算且通过质量门槛的适用模型（driver_dcf/pe/dcf/epv/eva/sotp），价值带不可用。"
        )
    elif value_source["band_note"]:
        data_gaps.append(value_source["band_note"])
    if resistance is None:
        data_gaps.append("技术证据缺少阻力位，技术目标不可用。")
    for name, reason in terminal_failures:
        data_gaps.append(f"估值 DCF 终值检查 {name} 判定 fail：{reason}")

    plan: dict[str, Any] = {
        "identity": {
            "issuer_id": valuation_identity["issuer_id"],
            "listing_id": valuation_identity["listing_id"],
            "case_id": case_id,
            "artifact_version": 1,
            "schema_version": 1,
        },
        "schema_version": 1,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "rules_version": rules["rules_version"],
        "computed_as_of": reference_now.isoformat(),
        "market_scope": market_scope,
        "currency": currency,
        "direction": "long_only",
        "horizon_months": {"min": rules["horizon_min"], "max": rules["horizon_max"]},
        "status": status,
        "gates": gates,
        "veto": veto,
        "entry_plan": entry_plan,
        "target_plan": target_plan,
        "invalidation_plan": invalidation_plan,
        "downside_pct": downside_pct,
        "reward_risk_ratio": reward_risk,
        "risk_triggers": thesis["falsification_conditions"],
        "references": {
            "valuation_id": (
                f"valuation:{case_id}:v{valuation_identity['artifact_version']}"
            ),
            "evidence_id": evidence_id,
            "earnings_quality_id": (
                f"earnings-quality:{case_id}:v{earnings_identity['artifact_version']}"
            ),
        },
        "price_as_of": evidence_as_of_text,
        "technical_valid_until": technical_valid_until.isoformat(),
        "data_gaps": data_gaps,
    }
    if ah_compare is not None:
        plan["ah_compare"] = ah_compare
    if vie_adr is not None:
        plan["vie_adr"] = vie_adr
    return plan


def _fmt(value: float) -> str:
    return json.dumps(_rounded(value))


def _render_html(plan: dict[str, Any], evidence: dict[str, Any]) -> str:
    bars = evidence.get("ohlcv")
    if not isinstance(bars, list) or not bars:
        raise TradePlanError("technical evidence ohlcv must be a non-empty list")
    candles: list[dict[str, float]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            raise TradePlanError("technical evidence ohlcv bar must be an object")
        candles.append(
            {
                "open": _number(bar.get("open"), "ohlcv open"),
                "high": _number(bar.get("high"), "ohlcv high"),
                "low": _number(bar.get("low"), "ohlcv low"),
                "close": _number(bar.get("close"), "ohlcv close"),
            }
        )

    entry_zone = plan["entry_plan"]["zone"]
    value_band = plan["entry_plan"]["value_band"]
    invalidation = plan["invalidation_plan"]["technical_invalidation"]["level"]
    technical_target = plan["target_plan"]["technical_target"]["level"]
    fundamental_target = plan["target_plan"]["fundamental_target"]["level"]

    prices = [bar["high"] for bar in candles] + [bar["low"] for bar in candles]
    prices += [
        value_band["low"],
        value_band["high"],
        entry_zone["low"],
        entry_zone["high"],
        invalidation,
        fundamental_target,
    ]
    if technical_target is not None:
        prices.append(technical_target)
    price_min, price_max = min(prices), max(prices)
    span = (price_max - price_min) or 1.0
    price_min -= span * 0.05
    price_max += span * 0.05
    span = price_max - price_min

    width, height = 960, 540
    left, right, top, bottom = 60, 200, 40, 40
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x_at(index: int) -> float:
        return left + plot_w * (index + 0.5) / len(candles)

    def y_at(price: float) -> float:
        return top + plot_h * (price_max - price) / span

    parts: list[str] = []
    parts.append(
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" '
        'fill="#fafafa" stroke="#cccccc"/>'
    )

    def band(low: float, high: float, color: str, opacity: float) -> None:
        y_high, y_low = y_at(high), y_at(low)
        parts.append(
            f'<rect x="{left}" y="{y_high:.2f}" width="{plot_w}" '
            f'height="{(y_low - y_high):.2f}" fill="{color}" fill-opacity="{opacity}"/>'
        )

    band(value_band["low"], value_band["high"], "#4a90d9", 0.12)
    band(entry_zone["low"], entry_zone["high"], "#2e9e5b", 0.18)

    candle_w = max(2.0, plot_w / len(candles) * 0.6)
    for index, bar in enumerate(candles):
        x = x_at(index)
        color = "#2e9e5b" if bar["close"] >= bar["open"] else "#c0392b"
        parts.append(
            f'<line x1="{x:.2f}" y1="{y_at(bar["high"]):.2f}" x2="{x:.2f}" '
            f'y2="{y_at(bar["low"]):.2f}" stroke="{color}" stroke-width="1"/>'
        )
        body_top = y_at(max(bar["open"], bar["close"]))
        body_h = max(1.0, abs(y_at(bar["open"]) - y_at(bar["close"])))
        parts.append(
            f'<rect x="{(x - candle_w / 2):.2f}" y="{body_top:.2f}" '
            f'width="{candle_w:.2f}" height="{body_h:.2f}" fill="{color}"/>'
        )

    def level_line(price: float, color: str, label: str) -> None:
        y = y_at(price)
        parts.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" '
            f'stroke="{color}" stroke-width="1.5" stroke-dasharray="6 4"/>'
        )
        parts.append(
            f'<text x="{left + plot_w + 6}" y="{(y + 4):.2f}" font-size="12" '
            f'fill="{color}">{label} {_fmt(price)}</text>'
        )

    level_line(entry_zone["low"], "#2e9e5b", "入场区间下沿")
    level_line(entry_zone["high"], "#2e9e5b", "入场区间上沿")
    level_line(value_band["low"], "#4a90d9", "价值带下沿")
    level_line(value_band["high"], "#4a90d9", "价值带上沿")
    level_line(invalidation, "#c0392b", "技术失效")
    if technical_target is not None:
        level_line(technical_target, "#e08a00", "技术目标")
    level_line(fundamental_target, "#7d3c98", "基本面目标")

    symbol = escape(str(evidence.get("symbol", "")), quote=True)
    title = (
        f"交易方案注释图 {symbol} · 证据 as_of {plan['price_as_of']} · "
        f"技术有效期至 {plan['technical_valid_until']}"
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif">'
        f'<text x="{left}" y="24" font-size="14" fill="#333333">{title}</text>'
        + "".join(parts)
        + "</svg>"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8"/>\n'
        f"<title>交易方案注释图 {symbol}</title>\n</head>\n<body>\n"
        "<p>本图仅展示 trade-plan.json 与 technical-evidence.json 中已记录的数值，"
        "为条件式观察材料，不构成任何指令。</p>\n"
        f"{svg}\n</body>\n</html>\n"
    )


def _write_new(path: Path, content: str) -> None:
    resolved = path.resolve()
    if RUNTIME_ROOT == resolved or RUNTIME_ROOT in resolved.parents:
        raise TradePlanError("output path must not be inside the Skill runtime package")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--valuation", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--earnings-quality", required=True, type=Path)
    parser.add_argument("--thesis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--html", type=Path)
    parser.add_argument("--now")
    arguments = parser.parse_args()
    try:
        valuation = _load_json(arguments.valuation, "valuation input")
        evidence = _load_json(arguments.evidence, "technical evidence input")
        earnings_quality = _load_json(
            arguments.earnings_quality, "earnings-quality input"
        )
        thesis_payload = _load_json(arguments.thesis, "thesis input")
        rules = _load_rules()
        now = None
        if arguments.now is not None:
            _, now = _as_of_moment(arguments.now, "--now")
        plan = build_plan(
            valuation, evidence, earnings_quality, thesis_payload, rules, now
        )
        rendered = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        if TRADE_DIRECTIVE.search(rendered):
            raise TradePlanError("trade plan output contains a trade directive")
        _write_new(arguments.output, rendered)
        if arguments.html is not None:
            if plan["status"] == "entry_plan":
                html = _render_html(plan, evidence)
                if TRADE_DIRECTIVE.search(html):
                    raise TradePlanError("trade plan html contains a trade directive")
                _write_new(arguments.html, html)
            else:
                print(
                    "status is watch; trade-plan.html is only generated for "
                    "entry_plan and was not written.",
                    file=sys.stderr,
                )
    except (OSError, TradePlanError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
