#!/usr/bin/env python3
"""Render the v1.0.3 nine-chapter equity underwriting report.

Reads ``underwriting-inputs.json`` (schema_version 1) and writes
``underwriting.md`` plus, on request, a single-file offline
``underwriting.html`` reading view generated from the same rendered
structure. Embedded valuation / earnings-quality / trade-plan artifacts
are displayed as computed; the renderer never recomputes them.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html as html_lib
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any


class UnderwritingError(ValueError):
    """Reject incomplete evidence rather than inventing a research conclusion."""


CHAPTERS = (
    ("研究范围、预注册命题与交易结论", "research_scope_hypothesis_trade_conclusion"),
    ("公司、业务模式与价值驱动", "company_business_model_value_drivers"),
    ("行业结构、竞争与行业专属反证", "industry_structure_competition_counter_evidence"),
    ("管理层、治理与资本配置", "management_governance_capital_allocation"),
    ("财务、分部/KPI 与财报质量", "financials_kpis_earnings_quality"),
    ("预期差、催化剂、基准率与跟踪清单", "expectation_gap_catalysts_base_rate_tracking"),
    ("可复算估值与“现价定价了什么”", "reproducible_valuation_priced_in"),
    ("反方论证、事前风险预演与可证伪条件", "counter_thesis_premortem_falsification"),
    ("来源、数据对账、时间戳、假设与数据缺口", "sources_reconciliation_assumptions_data_gaps"),
)
MODES = {"initial": "首次承保", "earnings_update": "财报更新"}
PRIMARY_SOURCE_KINDS = {"sec_filing", "regulatory_filing", "issuer_ir", "exchange"}
RESEARCH_SOURCE_KINDS = PRIMARY_SOURCE_KINDS | {"issuer_announcement", "credible_media"}
VALUATION_ASSUMPTION_SOURCE_KINDS = {"valuation_assumption"}
ALL_SOURCE_KINDS = RESEARCH_SOURCE_KINDS | {"public_quote"} | VALUATION_ASSUMPTION_SOURCE_KINDS
TRADE_DIRECTIVE = re.compile(
    r"买入|卖出|增持|减持|加仓|减仓|建仓|平仓|下单|持仓比例|做空|沽空|卖空|"
    r"\bbuy\b|\bsell\b|\bshort\b|\bposition size\b|\bplace (?:an )?order\b",
    re.IGNORECASE,
)
RUNTIME_ROOT = Path(__file__).resolve().parents[3]
INDUSTRY_REGISTRY = Path(__file__).resolve().parent.parent / "reference" / "industry_registry.json"
MARKET_CONTRACTS = Path(__file__).resolve().parent.parent / "reference" / "market_contracts.json"
BASELINE_MIN_ANNUAL = 3
BASELINE_MIN_QUARTERS = 8
CORE_BASELINE_FIELDS = ("revenue", "net_income", "operating_cash_flow")
CORE_BASELINE_LABELS = {"revenue": "收入", "net_income": "净利润", "operating_cash_flow": "经营现金流"}
FUNDAMENTAL_TARGET_LABELS = {
    "probability_weighted": "概率加权公允价值",
    "driver_dcf": "驱动型 DCF 概率加权每股价值",
    "pe": "PE 概率加权每股参考价值",
    "epv": "EPV 每股公允价值",
    "eva": "剩余收益（EVA）每股公允价值",
    "sotp": "SOTP 分部加总每股公允价值",
}
DRIVER_QUALITY_STATUSES = {"usable", "conditional", "unreliable"}
EVIDENCE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
VALUATION_STATUSES = {"computed", "missing_inputs", "invalid_inputs", "not_applicable", "no_solution"}
EARNINGS_COMPONENT_STATUSES = {"computed", "missing", "not_applicable"}
TERMINAL_CHECK_NAMES = (
    "long_run_growth",
    "mature_margin",
    "reinvestment_roic_consistency",
)
TERMINAL_CHECK_STATUSES = {"pass", "warn", "fail"}


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnderwritingError(f"{context} requires text")
    return value.strip()


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnderwritingError(f"{context} requires a finite number")
    result = float(value)
    if not isfinite(result):
        raise UnderwritingError(f"{context} requires a finite number")
    return result


def _fmt(value: object, context: str = "value") -> str:
    """Format a number with up to six decimals, trailing zeros trimmed."""
    number = _number(value, context)
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _money(value: object, context: str = "value") -> str:
    return f"{_number(value, context):.2f}"


def _pct(value: object, context: str = "value") -> str:
    return f"{_number(value, context):.2%}"


def _as_of_moment(value: object, context: str) -> tuple[str, datetime]:
    text = _text(value, context)
    if "T" not in text:
        raise UnderwritingError(
            f"{context} requires a complete timestamp with timezone"
        )
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as error:
        raise UnderwritingError(f"{context} requires an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UnderwritingError(f"{context} timestamp requires a timezone")
    return text, parsed.astimezone(timezone.utc)


def _as_of(value: object, context: str) -> str:
    return _as_of_moment(value, context)[0]


def _market_contract(scopes: object, market_scope: str, currency: str, listing_id: str) -> None:
    if not isinstance(scopes, dict) or market_scope not in scopes:
        raise UnderwritingError(f"market_scope is not supported: {market_scope}")
    if market_scope == "ah_compare":
        allowed_currencies = {scopes["a_share"]["currency"], scopes["hk"]["currency"]}
        suffixes = tuple(
            suffix
            for name in ("a_share", "hk")
            for suffix in scopes[name].get("suffixes", [])
            if suffix
        )
        bare = False
    else:
        contract = scopes[market_scope]
        allowed_currencies = {contract["currency"]}
        suffixes = tuple(suffix for suffix in contract.get("suffixes", []) if suffix)
        bare = "" in contract.get("suffixes", [])
    if currency not in allowed_currencies:
        raise UnderwritingError(
            f"market_scope {market_scope} does not allow currency {currency}"
        )
    if suffixes and listing_id.endswith(suffixes):
        return
    if bare:
        foreign = tuple(
            suffix
            for name, contract in scopes.items()
            if name != market_scope and isinstance(contract, dict)
            for suffix in contract.get("suffixes", [])
            if suffix
        )
        if not any(listing_id.endswith(suffix) for suffix in foreign):
            return
    raise UnderwritingError(
        f"listing_id {listing_id} suffix does not match market_scope {market_scope}"
    )


def _validate_source_times(value: object, research_as_of: datetime) -> None:
    if isinstance(value, dict):
        if {"name", "as_of", "url"}.issubset(value):
            _, source_as_of = _as_of_moment(value["as_of"], "source")
            if source_as_of > research_as_of:
                raise UnderwritingError("source as_of is after research as_of")
        for nested in value.values():
            _validate_source_times(nested, research_as_of)
    elif isinstance(value, list):
        for nested in value:
            _validate_source_times(nested, research_as_of)


def _source(source: object, context: str, allowed_kinds: set[str]) -> str:
    if not isinstance(source, dict):
        raise UnderwritingError(f"{context} requires a source")
    name = _text(source.get("name"), f"{context} source")
    kind = _text(source.get("kind"), f"{context} source")
    if kind not in allowed_kinds:
        raise UnderwritingError(f"{context} source kind is not allowed: {kind}")
    as_of = _as_of(source.get("as_of"), f"{context} source")
    url = _text(source.get("url"), f"{context} source")
    return f"[{name}]({url})（as_of：{as_of}）"


def _identity_record(
    value: object, context: str, required: tuple[str, ...]
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise UnderwritingError(f"{context} requires an object")
    return {field: _text(value.get(field), f"{context} {field}") for field in required}


def _identity(identity: object, symbol: str) -> tuple[dict[str, str], str]:
    if not isinstance(identity, dict) or identity.get("status") != "verified":
        reason = _text(
            identity.get("reason") if isinstance(identity, dict) else None,
            "unverified identity",
        )
        raise UnderwritingError(f"issuer identity is not uniquely verified: {reason}")
    requested = _identity_record(
        identity.get("request"), "identity request", ("company_name", "ticker", "exchange")
    )
    verified = _identity_record(
        identity.get("verified"),
        "verified identity",
        ("company_name", "ticker", "exchange", "issuer"),
    )
    if requested["ticker"] != symbol:
        raise UnderwritingError("identity request ticker does not match symbol")
    for field in ("company_name", "ticker", "exchange"):
        if requested[field] != verified[field]:
            raise UnderwritingError(f"verified identity {field} does not match request")
    return verified, _source(identity.get("source"), "issuer identity", PRIMARY_SOURCE_KINDS)


def _statement(value: object, context: str) -> str:
    statement = _text(value, context)
    if TRADE_DIRECTIVE.search(statement):
        raise UnderwritingError(f"{context} contains a trade directive")
    return statement


def _evidence(title: str, items: object) -> list[str]:
    if items is None or items == []:
        return [f"数据不可用：未提供{title}的可核验证据。", ""]
    if not isinstance(items, list):
        raise UnderwritingError(f"{title} must be a list")
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise UnderwritingError(f"{title} evidence item must be an object")
        label = _statement(item.get("label"), f"{title} evidence")
        statement = _statement(item.get("statement"), f"{label} statement")
        lines.append(
            f"- **{label}**：{statement}（来源：{_source(item.get('source'), label, RESEARCH_SOURCE_KINDS)}）"
        )
    lines.append("")
    return lines


def _verify_identity(name: str, artifact_identity: object, identity: dict[str, str]) -> None:
    if not isinstance(artifact_identity, dict):
        raise UnderwritingError(f"{name} identity requires an object")
    for field in ("issuer_id", "listing_id", "case_id"):
        value = _text(artifact_identity.get(field), f"{name} identity {field}")
        if value != identity[field]:
            raise UnderwritingError(
                f"{name} identity {field} mismatch: {value} != {identity[field]}"
            )
    for field in ("artifact_version", "schema_version"):
        number = artifact_identity.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or number != 1:
            raise UnderwritingError(f"{name} identity {field} must be 1")


def _artifact(payload: object, name: str, identity: dict[str, str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UnderwritingError(f"{name} requires an embedded computed artifact object")
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise UnderwritingError(f"{name} artifact schema_version must be 1")
    _verify_identity(name, payload.get("identity"), identity)
    return payload


def _industry_entry(industry: object) -> dict[str, Any]:
    if not isinstance(industry, dict):
        raise UnderwritingError("industry requires an object")
    industry_id = _text(industry.get("industry_id"), "industry")
    try:
        registry = json.loads(INDUSTRY_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UnderwritingError(f"industry registry is unavailable: {error}") from error
    for entry in registry.get("industries", []):
        if isinstance(entry, dict) and entry.get("id") == industry_id:
            return entry
    raise UnderwritingError(f"industry_id is not in the industry registry: {industry_id}")


def _industry_lines(entry: dict[str, Any]) -> list[str]:
    name = _text(entry.get("name_zh"), "industry entry")
    lines = [
        "### 行业注册表摘要",
        f"- 行业：{name}（id：{entry['id']}）",
        f"- 关键 KPI：{'、'.join(_text(item, 'industry kpi') for item in entry.get('key_kpis', []))}",
        f"- 预测驱动：{'、'.join(_text(item, 'industry driver') for item in entry.get('forecast_drivers', []))}",
        f"- 适用估值方法：{', '.join(_text(item, 'industry valuation method') for item in entry.get('valuation_methods', []))}",
        "- 行业专属反证框架：",
    ]
    lines.extend(
        f"  - {_statement(item, 'industry counter evidence')}"
        for item in entry.get("counter_evidence", [])
    )
    min_data = entry.get("min_data", {})
    if isinstance(min_data, dict) and min_data:
        lines.append(
            f"- 最低数据要求：年度 {_number(min_data.get('annual_years'), 'industry min_data'):.0f} 年、"
            f"季度 {_number(min_data.get('quarters'), 'industry min_data'):.0f} 个。"
        )
    lines.append("")
    return lines


def _baseline_table(title: str, rows: object) -> tuple[list[str], list[str]]:
    """Render a baseline table; returns (lines, extra_kpi_columns)."""
    if not isinstance(rows, list):
        raise UnderwritingError(f"{title} baseline must be a list")
    extra_columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise UnderwritingError(f"{title} baseline row must be an object")
        for key in row:
            if key not in CORE_BASELINE_FIELDS and key != "period" and key not in extra_columns:
                extra_columns.append(key)
    header = ["期间", *(CORE_BASELINE_LABELS[field] for field in CORE_BASELINE_FIELDS), *extra_columns]
    lines = [
        f"### {title}",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows:
        period = _text(row.get("period"), f"{title} baseline period")
        cells = [period]
        for field in CORE_BASELINE_FIELDS:
            value = row.get(field)
            cells.append(_fmt(value, f"{title} baseline {period} {field}") if value is not None else "未获取到")
        for column in extra_columns:
            value = row.get(column)
            cells.append(_fmt(value, f"{title} baseline {period} {column}") if value is not None else "未获取到")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines, extra_columns


def _baseline_gaps(baseline: object) -> tuple[list[str], list[str]]:
    """Validate the accounting baseline; returns (table_lines, gap_messages)."""
    if not isinstance(baseline, dict):
        raise UnderwritingError("accounting_baseline requires an object")
    annual_keys = [key for key in ("annual", "annual_years") if key in baseline]
    if len(annual_keys) > 1:
        raise UnderwritingError(
            "accounting_baseline must not define both annual and annual_years"
        )
    annual = baseline.get(annual_keys[0], []) if annual_keys else []
    quarters = baseline.get("quarters", [])
    lines: list[str] = []
    gaps: list[str] = []
    annual_lines, _ = _baseline_table("年度基线", annual)
    quarter_lines, _ = _baseline_table("季度基线", quarters)
    lines.extend(annual_lines)
    lines.extend(quarter_lines)
    if len(annual) < BASELINE_MIN_ANNUAL:
        gaps.append(
            f"会计基线缺口：年度基线仅 {len(annual)} 年，低于首次承保要求的 {BASELINE_MIN_ANNUAL} 年；未伪造缺失年度。"
        )
    if len(quarters) < BASELINE_MIN_QUARTERS:
        gaps.append(
            f"会计基线缺口：季度基线仅 {len(quarters)} 个，低于首次承保要求的 {BASELINE_MIN_QUARTERS} 个；未伪造缺失季度。"
        )
    return lines, gaps


def _earnings_quality_lines(earnings_quality: dict[str, Any]) -> list[str]:
    grade = _text(earnings_quality.get("grade"), "earnings-quality grade")
    if grade not in {"A", "B", "C", "D"}:
        raise UnderwritingError("earnings-quality grade must be A, B, C, or D")
    long_entry_veto = earnings_quality.get("long_entry_veto")
    if not isinstance(long_entry_veto, bool):
        raise UnderwritingError("earnings-quality long_entry_veto requires a boolean")
    if long_entry_veto != (grade in {"C", "D"}):
        raise UnderwritingError(
            "earnings-quality long_entry_veto contradicts grade: "
            f"grade={grade} but long_entry_veto={long_entry_veto}"
        )
    provisional = earnings_quality.get("provisional") is True
    lines = [
        "### 财报质量结论",
        f"- 财报质量级别：**{grade}**（{'暂定级别' if provisional else '非暂定'}"
        + (f"：{_statement(earnings_quality.get('provisional_reason'), 'provisional reason')}" if provisional and earnings_quality.get("provisional_reason") else "")
        + f"；rules_version：{_text(earnings_quality.get('rules_version'), 'earnings-quality rules_version')}）",
    ]
    if long_entry_veto:
        reason = _statement(earnings_quality.get("veto_reason"), "earnings-quality veto reason")
        lines.append(f"- 多头入场否决：已触发（C/D 级别确定性否决多头 entry_plan）：{reason}")
    else:
        lines.append("- 多头入场否决：未触发。")
    components = earnings_quality.get("components")
    if not isinstance(components, dict):
        raise UnderwritingError("earnings-quality components requires an object")
    lines.extend(["", "### 组件摘要", "| 组件 | 状态 | 得分 | 摘要 |", "| --- | --- | ---: | --- |"])
    for name, component in components.items():
        if not isinstance(component, dict):
            raise UnderwritingError(f"earnings-quality component {name} must be an object")
        status = _text(component.get("status"), f"earnings-quality component {name}")
        if status not in EARNINGS_COMPONENT_STATUSES:
            raise UnderwritingError(
                f"earnings-quality component {name} status is not supported: {status}"
            )
        score = component.get("score")
        score_text = _fmt(score, f"earnings-quality component {name} score") if score is not None else "—"
        summary = ""
        if name == "beneish" and component.get("m_score") is not None:
            summary = f"M-Score {_fmt(component['m_score'])}（阈值 {_fmt(component.get('threshold'), 'beneish threshold')}）"
        elif name == "revenue_recognition":
            flags = component.get("red_flags", [])
            triggered = [flag.get("id") for flag in flags if isinstance(flag, dict) and flag.get("triggered")]
            summary = f"触发红旗 {len(triggered)} 项" + (f"：{', '.join(triggered)}" if triggered else "")
        else:
            evidence = component.get("evidence") or component.get("signals") or []
            if isinstance(evidence, list) and evidence:
                last = evidence[-1]
                summary = _statement(last, f"earnings-quality component {name} summary") if isinstance(last, str) else ""
        lines.append(f"| {name} | {status} | {score_text} | {summary} |")
    lines.append("")
    return lines


def _model_status_line(name: str, result: object) -> str:
    if not isinstance(result, dict):
        return f"| {name} | missing | 未提供该模型结果。 |"
    status = _text(result.get("status"), f"valuation model {name}")
    if status not in VALUATION_STATUSES:
        raise UnderwritingError(
            f"valuation model {name} status is not supported: {status}"
        )
    if status == "computed":
        metrics = {
            "dcf": ("概率加权每股公允价值", result.get("probability_weighted_per_share"), _fmt),
            "driver_dcf": ("驱动型 DCF 概率加权每股", result.get("probability_weighted_per_share"), _fmt),
            "pe": ("PE 概率加权每股", result.get("probability_weighted_per_share"), _fmt),
            "reverse_dcf": ("现价隐含 FCF 年化增长", result.get("implied_fcf_cagr"), _pct),
            "pvgo": ("PVGO 占现价比例", result.get("pvgo_share_of_price"), _pct),
            "epv": ("EPV 每股", result.get("epv_per_share"), _fmt),
            "eva": ("剩余收益每股", result.get("residual_income_per_share"), _fmt),
            "sotp": ("SOTP 每股", result.get("per_share"), _fmt),
        }
        if name == "monte_carlo":
            percentiles = result.get("percentiles", {})
            detail = (
                f"p10/p50/p90：{_fmt(percentiles.get('p10'))} / "
                f"{_fmt(percentiles.get('p50'))} / {_fmt(percentiles.get('p90'))}"
                f"（seed {result.get('seed')}，trials {result.get('trials')}）"
            )
        elif name in metrics:
            label, value, formatter = metrics[name]
            detail = f"{label}：{formatter(value, f'valuation {name}')}"
        else:
            detail = _statement(result.get("detail"), f"valuation {name} detail") if result.get("detail") else "已计算。"
        return f"| {name} | {status} | {detail} |"
    if status == "not_applicable":
        reason = _statement(result.get("reason"), f"valuation {name} not_applicable reason")
        return f"| {name} | not_applicable | {reason} |"
    if status == "missing_inputs":
        missing = result.get("missing", [])
        missing_text = ", ".join(_text(item, f"valuation {name} missing") for item in missing) if isinstance(missing, list) else ""
        return f"| {name} | missing_inputs | 缺少必要输入：{missing_text}；不输出公允价值数字。 |"
    if status == "no_solution":
        detail = _statement(result.get("detail"), f"valuation {name} detail") if result.get("detail") else "反向 DCF 无解。"
        return f"| {name} | no_solution | {detail} |"
    return f"| {name} | {status} | 状态原样记录。 |"


def _validate_terminal_checks(valuation: dict[str, Any], trade_plan: dict[str, Any]) -> None:
    """Keep a forged entry plan from bypassing a failed/unknown DCF terminal check."""
    results = valuation.get("results")
    failures: list[str] = []
    for model in ("dcf", "driver_dcf"):
        entry = results.get(model) if isinstance(results, dict) else None
        if not isinstance(entry, dict) or entry.get("status") != "computed":
            continue
        checks = entry.get("terminal_value_checks")
        if not isinstance(checks, dict):
            raise UnderwritingError(f"computed {model} requires terminal_value_checks")
        for name in TERMINAL_CHECK_NAMES:
            check = checks.get(name)
            if not isinstance(check, dict):
                failures.append(f"{model}.{name}")
                continue
            status = check.get("status")
            if status not in TERMINAL_CHECK_STATUSES:
                raise UnderwritingError(f"terminal check {name} status is not supported: {status}")
            if status == "fail":
                failures.append(f"{model}.{name}")
    if not failures:
        return
    gates = trade_plan.get("gates")
    valuation_gate = gates.get("valuation") if isinstance(gates, dict) else None
    if trade_plan.get("status") == "entry_plan" or not isinstance(valuation_gate, dict) or valuation_gate.get("pass") is not False:
        raise UnderwritingError(
            "trade-plan valuation gate must fail when DCF terminal checks fail: "
            + ", ".join(failures)
        )


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None


def _provenance_record(provenance: object, field: str) -> dict[str, Any] | None:
    if not isinstance(provenance, dict):
        return None
    record = provenance.get(field)
    return record if isinstance(record, dict) else None


def _provenance_scalar(record: dict[str, Any] | None) -> float | None:
    return _finite_number(record.get("value")) if isinstance(record, dict) else None


def _provenance_list(record: dict[str, Any] | None) -> list[float] | None:
    value = record.get("value") if isinstance(record, dict) else None
    if not isinstance(value, list) or not value:
        return None
    numbers = [_finite_number(item) for item in value]
    if any(item is None for item in numbers):
        return None
    return [item for item in numbers if item is not None]


def _provenance_source_text(record: dict[str, Any] | None, context: str) -> str:
    """Render a provenance source via the shared source whitelist; incomplete
    or absent metadata fails closed to 未获取到 instead of being invented."""
    source = record.get("source") if isinstance(record, dict) else None
    if not isinstance(source, dict):
        return "未获取到"
    if any(
        not isinstance(source.get(field), str) or not str(source.get(field)).strip()
        for field in ("name", "kind", "as_of", "url")
    ):
        return "未获取到"
    return _source(source, context, ALL_SOURCE_KINDS)


def _provenance_meta_text(
    record: dict[str, Any] | None, field: str, context: str
) -> str:
    value = record.get(field) if isinstance(record, dict) else None
    if not isinstance(value, str) or not value.strip():
        return "未获取到"
    return _statement(value, context)


DCF_PROVENANCE_SCALAR_FIELDS = (
    ("price", "现价"),
    ("shares_outstanding", "总股本"),
    ("net_debt", "净债务"),
    ("wacc", "WACC"),
    ("terminal_growth", "永续增长率"),
    ("long_run_growth_cap", "长期增长上限"),
    ("mature_margin_benchmark", "成熟期利润率基准"),
)
DCF_PROVENANCE_PERCENT_FIELDS = {
    "wacc",
    "terminal_growth",
    "long_run_growth_cap",
    "mature_margin_benchmark",
}
DCF_PROVENANCE_SCENARIO_NAMES = ("bear", "base", "bull")
DCF_PROVENANCE_SCENARIO_FIELDS = (
    ("probability", "概率"),
    ("free_cash_flows", "FCF 路径"),
    ("margins", "利润率路径"),
    ("reinvestment_rate", "再投资率"),
    ("roic", "ROIC"),
)


def _dcf_inputs_lines(dcf: dict[str, Any], currency: str) -> list[str]:
    """Render DCF key inputs and scenario assumptions read verbatim from the
    valuation artifact's ``inputs_provenance`` — value plus source (name/URL/
    as_of), derivation and accounting_period per field. Missing values or
    metadata fail closed to 未获取到; nothing is recomputed or invented."""
    provenance = dcf.get("inputs_provenance")
    lines = [
        "### DCF 关键输入与情景假设",
        "以下数值与来源逐项读自估值工件 inputs_provenance（渲染器不重算）；缺失字段或元数据明示未获取到。",
    ]
    for field, label in DCF_PROVENANCE_SCALAR_FIELDS:
        record = _provenance_record(provenance, field)
        number = _provenance_scalar(record)
        if number is None:
            text = "未获取到"
        elif field in DCF_PROVENANCE_PERCENT_FIELDS:
            text = _pct(number, f"dcf inputs_provenance {field}")
        elif field == "price":
            text = f"{_fmt(number, 'dcf inputs_provenance price')} {currency}"
        else:
            text = _fmt(number, f"dcf inputs_provenance {field}")
        lines.append(
            f"- {label}（{field}）：{text}"
            f"；来源：{_provenance_source_text(record, f'dcf inputs_provenance {field}')}"
            f"；推导：{_provenance_meta_text(record, 'derivation', f'dcf {field} derivation')}"
            f"；会计期：{_provenance_meta_text(record, 'accounting_period', f'dcf {field} accounting_period')}"
        )
    scenarios = provenance.get("scenarios") if isinstance(provenance, dict) else None
    lines.extend([
        "",
        "| 情景 | 概率 | FCF 路径 | 利润率路径 | 再投资率 | ROIC |",
        "| --- | ---: | --- | --- | ---: | ---: |",
    ])
    scenario_meta: list[str] = []
    for name in DCF_PROVENANCE_SCENARIO_NAMES:
        record = scenarios.get(name) if isinstance(scenarios, dict) else None
        cells: list[str] = []
        for field, _label in DCF_PROVENANCE_SCENARIO_FIELDS:
            field_record = _provenance_record(record, field)
            if field in {"free_cash_flows", "margins"}:
                numbers = _provenance_list(field_record)
                if numbers is None:
                    cells.append("未获取到")
                elif field == "margins":
                    cells.append(
                        ", ".join(_pct(item, f"dcf {name} margins") for item in numbers)
                    )
                else:
                    cells.append(
                        ", ".join(
                            _fmt(item, f"dcf {name} free_cash_flows") for item in numbers
                        )
                    )
            else:
                number = _provenance_scalar(field_record)
                cells.append(
                    _pct(number, f"dcf {name} {field}") if number is not None else "未获取到"
                )
            scenario_meta.append(
                f"- {name}.{field}：来源：{_provenance_source_text(field_record, f'dcf {name} {field}')}"
                f"；推导：{_provenance_meta_text(field_record, 'derivation', f'dcf {name} {field} derivation')}"
                f"；会计期：{_provenance_meta_text(field_record, 'accounting_period', f'dcf {name} {field} accounting_period')}"
            )
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.extend(["", "情景假设来源与推导（逐项读自 inputs_provenance.scenarios，不重算）："])
    lines.extend(scenario_meta)
    lines.append("")
    return lines


def _driver_dcf_quality(driver: object) -> str | None:
    """Read the generic driver-DCF quality gate; fail closed on unknown states."""
    if not isinstance(driver, dict):
        return None
    quality = driver.get("quality")
    if not isinstance(quality, dict):
        if driver.get("status") == "computed":
            raise UnderwritingError("computed driver_dcf requires a quality object")
        return None
    status = _text(quality.get("status"), "driver_dcf quality")
    if status not in DRIVER_QUALITY_STATUSES:
        raise UnderwritingError(
            f"driver_dcf quality status is not supported: {status}"
        )
    if driver.get("status") == "computed":
        checks = driver.get("terminal_value_checks")
        if not isinstance(checks, dict):
            raise UnderwritingError("computed driver_dcf requires terminal_value_checks")
        for name in TERMINAL_CHECK_NAMES:
            check = checks.get(name)
            if not isinstance(check, dict):
                raise UnderwritingError(f"driver_dcf terminal check {name} is missing")
            check_status = check.get("status")
            if check_status not in TERMINAL_CHECK_STATUSES:
                raise UnderwritingError(
                    f"driver_dcf terminal check {name} status is not supported: {check_status}"
                )
            if status == "usable" and check_status != "pass":
                raise UnderwritingError(
                    f"usable driver_dcf requires terminal check {name} to pass"
                )
    return status


def _driver_dcf_lines(driver: dict[str, Any], currency: str) -> list[str]:
    """Render the driver-based DCF layered by its generic quality gate.

    Only a ``usable`` gate earns the 定制 DCF 参考值 framing; conditional
    outputs stay 条件性模型输出 and unreliable ones 估值模型待重建 — the
    baseline DCF number is never dressed up as a fundamental target."""
    quality_status = _driver_dcf_quality(driver)
    quality = driver.get("quality") if isinstance(driver.get("quality"), dict) else {}
    lines = [
        "### 驱动型 DCF（经营驱动逐段推导现金流）",
        f"- 模型：{_text(driver.get('model_kind'), 'driver_dcf model_kind')}"
        f"（model_version：{_text(driver.get('model_version'), 'driver_dcf model_version')}）。",
        f"- 公式：{_statement(driver.get('formula'), 'driver_dcf formula') if driver.get('formula') else 'NOPAT = revenue × operating_margin × (1 − tax_rate)；FCF = NOPAT + D&A − capex − ΔNWC。'}",
    ]
    if quality_status is not None:
        reasons = quality.get("reasons", [])
        reason_text = (
            "；".join(_statement(item, "driver_dcf quality reason") for item in reasons)
            if isinstance(reasons, list) and reasons
            else "未记录原因"
        )
        lines.append(f"- 质量门槛：**{quality_status}**——{reason_text}。")
    status = driver.get("status")
    if status != "computed":
        detail = driver.get("detail")
        missing = driver.get("missing")
        if isinstance(missing, list) and missing:
            lines.append(
                "- 状态："
                + _text(status, "driver_dcf status")
                + "；缺少输入："
                + ", ".join(_text(item, "driver_dcf missing") for item in missing)
                + "；不输出参考值。"
            )
        else:
            lines.append(
                f"- 状态：{_text(status, 'driver_dcf status')}"
                + (f"——{_statement(detail, 'driver_dcf detail')}" if detail else "")
            )
        lines.append("")
        return lines
    scenarios = driver.get("scenarios", [])
    lines.extend([
        "| 情景 | 概率 | 预测期 | 推导 FCF 路径 | 企业价值 | 股权价值 | 每股价值 |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: |",
    ])
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise UnderwritingError("driver_dcf scenario must be an object")
        flows = scenario.get("free_cash_flows")
        flow_text = (
            ", ".join(_fmt(item, "driver_dcf fcf") for item in flows)
            if isinstance(flows, list)
            else "未获取到"
        )
        lines.append(
            f"| {_text(scenario.get('name'), 'driver_dcf scenario')} "
            f"| {_pct(scenario.get('probability'), 'driver_dcf probability')} "
            f"| {_number(scenario.get('forecast_periods'), 'driver_dcf periods'):.0f} "
            f"| {flow_text} "
            f"| {_fmt(scenario.get('enterprise_value'), 'driver_dcf enterprise_value')} "
            f"| {_fmt(scenario.get('equity_value'), 'driver_dcf equity_value')} "
            f"| {_fmt(scenario.get('per_share'), 'driver_dcf per_share')} |"
        )
    weighted = _number(
        driver.get("probability_weighted_per_share"), "driver_dcf weighted"
    )
    zone = driver.get("value_zone")
    zone_text = (
        f"{_fmt(zone.get('low'), 'driver_dcf zone low')} – "
        f"{_fmt(zone.get('high'), 'driver_dcf zone high')} {currency}"
        if isinstance(zone, dict)
        else "未计算"
    )
    lines.append("")
    if quality_status == "usable":
        lines.append(
            f"定制 DCF 参考值（可作为基本面目标候选）：**{_fmt(weighted)} {currency}**；"
            f"参考区间：{zone_text}。"
        )
    elif quality_status == "conditional":
        lines.append(
            f"条件性模型输出：{_fmt(weighted)} {currency}（参考区间 {zone_text}）；"
            "质量门槛为 conditional，未形成基本面目标。"
        )
    else:
        lines.append(
            "估值模型待重建：质量门槛为 unreliable，"
            "以上数值仅为留档模型输出，不构成基本面目标。"
        )
    assumptions = driver.get("terminal_assumptions")
    if isinstance(assumptions, dict):
        lines.append(
            f"- 终值假设：永续增长率 {_pct(assumptions.get('terminal_growth'), 'driver_dcf terminal_growth')}，"
            f"WACC {_pct(assumptions.get('wacc'), 'driver_dcf wacc')}；"
            f"{_statement(assumptions.get('detail'), 'driver_dcf terminal detail') if assumptions.get('detail') else ''}"
        )
    checks = driver.get("terminal_value_checks")
    if not isinstance(checks, dict):
        raise UnderwritingError("computed driver_dcf requires terminal_value_checks")
    for check_name in TERMINAL_CHECK_NAMES:
        check = checks.get(check_name)
        if not isinstance(check, dict):
            raise UnderwritingError(f"driver_dcf terminal check {check_name} is missing")
        lines.append(
            f"- **{check_name}**：{_text(check.get('status'), f'driver check {check_name}')}——"
            f"{_statement(check.get('detail'), f'driver check {check_name}')}"
        )
    lines.append("")
    return lines


def _pe_quality_status(pe: object) -> str | None:
    """Read the PE quality gate; computed PE must carry an explicit gate."""
    if not isinstance(pe, dict):
        return None
    quality = pe.get("quality")
    if not isinstance(quality, dict):
        if pe.get("status") == "computed":
            raise UnderwritingError("computed pe requires a quality object")
        return None
    status = _text(quality.get("status"), "pe quality")
    if status not in DRIVER_QUALITY_STATUSES:
        raise UnderwritingError(f"pe quality status is not supported: {status}")
    return status


def _pe_lines(pe: dict[str, Any], currency: str) -> list[str]:
    """Render an explicit scenario P/E model without implying certainty."""
    quality_status = _pe_quality_status(pe)
    basis = pe.get("earnings_basis")
    basis_text = _text(basis, "pe earnings_basis") if basis is not None else "未获取到"
    basis_rationale = pe.get("basis_rationale")
    rationale_text = (
        _statement(basis_rationale, "pe basis rationale")
        if basis_rationale is not None
        else "未获取到"
    )
    lines = [
        "### P/E 估值（EPS × P/E 倍数）",
        f"- 盈利口径：{basis_text}；口径说明：{rationale_text}。",
    ]
    quality = pe.get("quality") if isinstance(pe.get("quality"), dict) else {}
    reasons = quality.get("reasons", [])
    if quality_status is not None:
        reason_text = (
            "；".join(_statement(item, "pe quality reason") for item in reasons)
            if isinstance(reasons, list) and reasons
            else "未记录原因"
        )
        lines.append(f"- 质量门槛：**{quality_status}**——{reason_text}。")
    status = _text(pe.get("status"), "pe status")
    if status != "computed":
        missing = pe.get("missing")
        if isinstance(missing, list) and missing:
            lines.append(
                f"- 状态：{status}；缺少输入："
                + ", ".join(_text(item, "pe missing") for item in missing)
                + "；不输出参考值。"
            )
        else:
            lines.append(f"- 状态：{status}。")
        lines.append("")
        return lines
    lines.extend([
        "| 情景 | 概率 | EPS | P/E 倍数 | 每股参考值 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    scenarios = pe.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise UnderwritingError("computed pe requires scenarios")
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise UnderwritingError("pe scenario must be an object")
        lines.append(
            f"| {_text(scenario.get('name'), 'pe scenario')} "
            f"| {_pct(scenario.get('probability'), 'pe probability')} "
            f"| {_fmt(scenario.get('eps'), 'pe eps')} "
            f"| {_fmt(scenario.get('pe_multiple'), 'pe multiple')}x "
            f"| {_fmt(scenario.get('per_share'), 'pe per_share')} {currency} |"
        )
    weighted = _number(
        pe.get("probability_weighted_per_share"), "pe weighted per_share"
    )
    zone = pe.get("value_zone")
    zone_text = (
        f"{_fmt(zone.get('low'), 'pe zone low')} – "
        f"{_fmt(zone.get('high'), 'pe zone high')} {currency}"
        if isinstance(zone, dict)
        else "未计算"
    )
    lines.append("")
    if quality_status == "usable":
        lines.append(
            f"PE 参考值（可作为基本面目标候选）：**{_fmt(weighted)} {currency}**；"
            f"参考区间：{zone_text}。"
        )
    elif quality_status == "conditional":
        lines.append(
            f"条件性 PE 输出：{_fmt(weighted)} {currency}（参考区间 {zone_text}）；"
            "质量门槛为 conditional，未形成基本面目标。"
        )
    else:
        lines.append(
            "估值模型待重建：PE 质量门槛为 unreliable，以上数值仅为留档输出。"
        )
    if pe.get("current_pe") is not None:
        lines.append(
            f"- 当前价格对应的加权 P/E：{_fmt(pe.get('current_pe'), 'pe current_pe')}x。"
        )
    lines.append("")
    return lines


def _valuation_lines(valuation: dict[str, Any], currency: str) -> list[str]:
    results = valuation.get("results")
    if not isinstance(results, dict):
        raise UnderwritingError("valuation results requires an object")
    lines = [
        "### 估值引擎留档",
        f"- 引擎：{_text(valuation.get('engine'), 'valuation engine')}"
        f"（engine_version：{_text(valuation.get('engine_version'), 'valuation engine_version')}，"
        f"model_version：{_text(valuation.get('model_version'), 'valuation model_version')}，"
        f"computed_as_of：{_as_of(valuation.get('computed_as_of'), 'valuation computed_as_of')}）",
        "",
        "### 模型结果总览",
        "| 模型 | 状态 | 关键数值 / 原因 |",
        "| --- | --- | --- |",
    ]
    model_names = ["dcf"]
    if "driver_dcf" in results:
        model_names.append("driver_dcf")
    model_names.extend(["reverse_dcf", "pvgo"])
    if "pe" in results:
        model_names.append("pe")
    model_names.extend(["epv", "eva", "sotp", "monte_carlo"])
    for name in model_names:
        lines.append(_model_status_line(name, results.get(name)))
    lines.append("")
    driver = results.get("driver_dcf")
    driver_quality = _driver_dcf_quality(driver)
    driver_usable = (
        isinstance(driver, dict)
        and driver.get("status") == "computed"
        and driver_quality == "usable"
    )
    dcf = results.get("dcf")
    if isinstance(dcf, dict) and dcf.get("status") == "computed":
        scenarios = dcf.get("scenarios", [])
        lines.extend([
            "### 三情景概率加权 DCF",
            "| 情景 | 概率 | 企业价值 | 股权价值 | 每股估值 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise UnderwritingError("valuation dcf scenario must be an object")
            lines.append(
                f"| {_text(scenario.get('name'), 'dcf scenario')} "
                f"| {_pct(scenario.get('probability'), 'dcf scenario probability')} "
                f"| {_fmt(scenario.get('enterprise_value'), 'dcf enterprise_value')} "
                f"| {_fmt(scenario.get('equity_value'), 'dcf equity_value')} "
                f"| {_fmt(scenario.get('per_share'), 'dcf per_share')} |"
            )
        weighted = _number(dcf.get("probability_weighted_per_share"), "dcf probability_weighted_per_share")
        lines.append("")
        lines.append(f"概率加权每股公允价值：**{_fmt(weighted)} {currency}**。")
        zone = dcf.get("value_zone")
        if isinstance(zone, dict):
            lines.append(
                f"价值区间：{_fmt(zone.get('low'), 'value_zone low')} – "
                f"{_fmt(zone.get('high'), 'value_zone high')} {currency}"
                "（保守情景每股价值至概率加权值）。"
            )
        checks = dcf.get("terminal_value_checks")
        if isinstance(checks, dict):
            lines.extend(["", "### 终值三查"])
            for check_name in ("long_run_growth", "mature_margin", "reinvestment_roic_consistency"):
                check = checks.get(check_name)
                if isinstance(check, dict):
                    lines.append(
                        f"- **{check_name}**：{_text(check.get('status'), f'terminal check {check_name}')}——"
                        f"{_statement(check.get('detail'), f'terminal check {check_name}')}"
                    )
        if driver_usable:
            lines.append(
                "- 模型角色：baseline（可审计基线）；基本面参考值以质量门槛为 "
                "usable 的驱动型 DCF 为准。"
            )
        else:
            lines.append(
                "- 模型角色：baseline（可审计基线）。该现金流路径为直接给定、"
                "未由经营驱动逐项推导，按通用质量门槛不构成基本面目标。"
            )
        lines.append("")
    if isinstance(driver, dict):
        lines.extend(_driver_dcf_lines(driver, currency))
    pe = results.get("pe")
    if isinstance(pe, dict):
        lines.extend(_pe_lines(pe, currency))
    # DCF 关键输入节无论 dcf 状态（computed/missing_inputs/no_solution 等）
    # 都出现；无 inputs_provenance 时逐项“未获取到”。
    if isinstance(dcf, dict):
        lines.extend(_dcf_inputs_lines(dcf, currency))
    reverse = results.get("reverse_dcf")
    pvgo = results.get("pvgo")
    pe = results.get("pe")
    priced_in: list[str] = ["### 现价定价了什么"]
    if driver_usable:
        priced_in.append(
            "- 基本面参考值已由质量门槛为 usable 的驱动型 DCF 形成；"
            "以下市场隐含条件用于对照，不构成价值目标。"
        )
    else:
        priced_in.append(
            "- 未形成基本面目标：现金流路径须由经营驱动推导且质量门槛为 usable "
            "才具备资格；当前输出为条件性模型输出或估值模型待重建。"
        )
    if isinstance(reverse, dict) and reverse.get("status") == "computed":
        priced_in.append(
            f"- 反向 DCF：{_statement(reverse.get('detail'), 'reverse_dcf detail') if reverse.get('detail') else ''}"
            f"（隐含 FCF 年化增长 {_pct(reverse.get('implied_fcf_cagr'), 'reverse_dcf implied')}，"
            f"预测期 {_number(reverse.get('horizon_years'), 'reverse_dcf horizon'):.0f} 年）。"
        )
    else:
        priced_in.append("- 反向 DCF：见模型结果总览中的状态与原因。")
    if isinstance(pvgo, dict) and pvgo.get("status") == "computed":
        priced_in.append(
            f"- PVGO 分解：零增长价值每股 {_fmt(pvgo.get('no_growth_value_per_share'), 'pvgo no_growth')} {currency}，"
            f"PVGO 每股 {_fmt(pvgo.get('pvgo_per_share'), 'pvgo per_share')} {currency}，"
            f"PVGO 占现价 {_pct(pvgo.get('pvgo_share_of_price'), 'pvgo share')}。"
        )
    else:
        priced_in.append("- PVGO 分解：见模型结果总览中的状态与原因。")
    if isinstance(pe, dict) and pe.get("status") == "computed":
        pe_quality = _pe_quality_status(pe)
        if pe_quality == "usable":
            priced_in.append(
                f"- PE 参考值：概率加权每股 {_fmt(pe.get('probability_weighted_per_share'), 'pe weighted')} {currency}；"
                "仅在盈利口径与倍数质量门槛通过时作为基本面目标候选。"
            )
        else:
            priced_in.append(
                f"- PE：已计算但质量门槛为 {pe_quality or 'unknown'}，不构成价值目标。"
            )
    else:
        priced_in.append("- PE：未请求或未计算；需要显式的前瞻/正常化 EPS 与情景 P/E 倍数。")
    priced_in.append(
        "- 反向 DCF 与 PVGO 均为市场隐含条件与预期分解，仅供对照，不构成价值目标。"
    )
    priced_in.append("")
    lines.extend(priced_in)
    return lines


def _trade_plan_rules(trade_plan: dict[str, Any]) -> None:
    """Fail closed on trade-plan semantics the renderer relies on."""
    status = _text(trade_plan.get("status"), "trade-plan status")
    if status not in {"entry_plan", "watch"}:
        raise UnderwritingError("trade-plan status must be entry_plan or watch")
    direction = _text(trade_plan.get("direction"), "trade-plan direction")
    if direction != "long_only":
        raise UnderwritingError("trade-plan direction must be long_only")
    horizon = trade_plan.get("horizon_months")
    if not isinstance(horizon, dict):
        raise UnderwritingError("trade-plan horizon_months requires an object")
    horizon_min = _number(horizon.get("min"), "trade-plan horizon_months min")
    horizon_max = _number(horizon.get("max"), "trade-plan horizon_months max")
    if not 1 <= horizon_min <= horizon_max <= 6:
        raise UnderwritingError("trade-plan horizon_months must be within 1–6 months")
    gates = trade_plan.get("gates")
    if not isinstance(gates, dict) or not gates:
        raise UnderwritingError("trade-plan gates requires a non-empty object")
    for gate_name, gate in gates.items():
        if not isinstance(gate, dict) or not isinstance(gate.get("pass"), bool):
            raise UnderwritingError(f"trade-plan gate {gate_name} is invalid")
        _statement(gate.get("reason"), f"trade-plan gate {gate_name} reason")
    all_pass = all(gate["pass"] for gate in gates.values())
    veto = trade_plan.get("veto")
    if status == "entry_plan":
        if not all_pass or veto is not None:
            raise UnderwritingError(
                "trade-plan entry_plan requires all gates pass and veto=null"
            )
        entry = trade_plan.get("entry_plan")
        target = trade_plan.get("target_plan")
        invalidation = trade_plan.get("invalidation_plan")
        if not isinstance(entry, dict) or not isinstance(entry.get("zone"), dict):
            raise UnderwritingError("trade-plan entry_plan requires a zone")
        low = _number(entry["zone"].get("low"), "trade-plan entry zone low")
        high = _number(entry["zone"].get("high"), "trade-plan entry zone high")
        if low > high:
            raise UnderwritingError("trade-plan entry zone low must not exceed high")
        if not isinstance(target, dict) or not isinstance(invalidation, dict):
            raise UnderwritingError("trade-plan entry_plan target/invalidation structures are required")
        technical_target = target.get("technical_target")
        fundamental_target = target.get("fundamental_target")
        technical_invalidation = invalidation.get("technical_invalidation")
        if (
            not isinstance(technical_target, dict)
            or not isinstance(fundamental_target, dict)
            or fundamental_target.get("level") is None
            or not isinstance(technical_invalidation, dict)
            or technical_invalidation.get("level") is None
        ):
            raise UnderwritingError("trade-plan entry_plan target/invalidation values are incomplete")
    elif all_pass:
        raise UnderwritingError("trade-plan watch cannot have all gates pass")


def _technical_evidence(
    evidence_ref: dict[str, Any], identity: dict[str, str], base_dir: Path
) -> tuple[str, str]:
    evidence_id = _text(evidence_ref.get("evidence_id"), "technical_evidence_ref")
    if not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
        raise UnderwritingError(
            "technical_evidence_ref evidence_id must match sha256:<64 lowercase hex>"
        )
    evidence_path_text = _text(evidence_ref.get("artifact_path"), "technical_evidence_ref")
    path = Path(evidence_path_text)
    if path.is_absolute() or "\\" in evidence_path_text or ":" in evidence_path_text or ".." in path.parts:
        raise UnderwritingError(
            "technical_evidence_ref artifact_path must be a portable relative path: "
            f"{evidence_path_text}"
        )
    raw_candidate = base_dir / path
    # Check the link before resolving it.  Resolving first would erase the
    # symlink bit and allow a portable reference to escape through a link.
    if raw_candidate.is_symlink():
        raise UnderwritingError(
            f"technical evidence artifact must not be a symlink: {evidence_path_text}"
        )
    candidate = raw_candidate.resolve()
    try:
        candidate.relative_to(base_dir.resolve())
    except ValueError as error:
        raise UnderwritingError("technical_evidence_ref resolves outside the input directory") from error
    if not candidate.is_file():
        raise UnderwritingError(f"technical evidence artifact does not exist: {evidence_path_text}")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnderwritingError("technical evidence artifact is not readable JSON") from error
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        raise UnderwritingError("technical evidence artifact schema_version must be 1")
    _verify_identity("technical evidence artifact", payload.get("identity"), identity)
    payload_symbol = _text(payload.get("symbol"), "technical evidence artifact symbol")
    if payload_symbol != identity["listing_id"] and not (
        "." not in payload_symbol
        and identity["listing_id"] == f"{payload_symbol}.US"
    ):
        raise UnderwritingError(
            "technical evidence artifact symbol does not match identity listing_id"
        )
    if payload.get("evidence_id") != evidence_id:
        raise UnderwritingError("technical evidence evidence_id does not match reference")
    _, payload_as_of = _as_of_moment(payload.get("as_of"), "technical evidence artifact as_of")
    _, ref_as_of = _as_of_moment(evidence_ref.get("as_of"), "technical_evidence_ref")
    if payload_as_of != ref_as_of:
        raise UnderwritingError("technical evidence artifact as_of does not match reference")
    source = payload.get("source")
    if isinstance(source, dict) and source.get("as_of") is not None:
        _, source_as_of = _as_of_moment(
            source.get("as_of"), "technical evidence artifact source"
        )
        if source_as_of > ref_as_of:
            raise UnderwritingError(
                "technical evidence artifact source as_of is after evidence as_of"
            )
    return evidence_id, evidence_path_text


def _earnings_trade_gate(
    earnings_quality: dict[str, Any], trade_plan: dict[str, Any]
) -> None:
    """Fail closed when the earnings-quality veto and the trade plan disagree."""
    grade = _text(earnings_quality.get("grade"), "earnings-quality grade")
    if grade not in {"A", "B", "C", "D"}:
        raise UnderwritingError("earnings-quality grade must be A, B, C, or D")
    long_entry_veto = earnings_quality.get("long_entry_veto")
    if not isinstance(long_entry_veto, bool):
        raise UnderwritingError("earnings-quality long_entry_veto requires a boolean")
    if long_entry_veto != (grade in {"C", "D"}):
        raise UnderwritingError(
            "earnings-quality long_entry_veto contradicts grade: "
            f"grade={grade} but long_entry_veto={long_entry_veto}"
        )
    status = _text(trade_plan.get("status"), "trade-plan status")
    if long_entry_veto and status == "entry_plan":
        raise UnderwritingError(
            "earnings-quality grade C/D (long_entry_veto) forbids "
            "trade-plan status entry_plan"
        )


def _fundamental_target_label(basis: object) -> str:
    text = _text(basis, "fundamental target basis")
    label = FUNDAMENTAL_TARGET_LABELS.get(text)
    if label is None:
        raise UnderwritingError(f"fundamental target basis is not supported: {text}")
    return label


def _trade_conclusion_lines(trade_plan: dict[str, Any], currency: str) -> list[str]:
    status = _text(trade_plan.get("status"), "trade-plan status")
    horizon = trade_plan.get("horizon_months", {})
    lines = ["### 交易结论"]
    if status == "entry_plan":
        entry_plan = trade_plan.get("entry_plan")
        if not isinstance(entry_plan, dict):
            raise UnderwritingError("trade-plan entry_plan requires an object")
        zone = entry_plan.get("zone")
        if not isinstance(zone, dict):
            raise UnderwritingError("trade-plan entry zone requires an object")
        lines.append(
            f"- 方案状态：entry_plan（仅多头、条件式、持仓无关；研究/持有期 "
            f"{_number(horizon.get('min'), 'horizon min'):.0f}–{_number(horizon.get('max'), 'horizon max'):.0f} 个月）。"
        )
        lines.append(
            f"- 入场区间：{_money(zone.get('low'), 'entry zone low')} – "
            f"{_money(zone.get('high'), 'entry zone high')} {currency}"
            f"（依据：{_text(entry_plan.get('basis'), 'entry basis')}）。"
        )
        triggers = entry_plan.get("trigger_conditions", [])
        if isinstance(triggers, list) and triggers:
            lines.append("- 触发条件（全部满足才生效）：")
            lines.extend(f"  - {_statement(item, 'trigger condition')}" for item in triggers)
        target = trade_plan.get("target_plan", {})
        if isinstance(target, dict) and target:
            technical = target.get("technical_target", {})
            fundamental = target.get("fundamental_target", {})
            technical_level = technical.get("level")
            technical_text = (
                _money(technical_level, "technical target")
                if technical_level is not None
                else "不可用"
            )
            label = _fundamental_target_label(fundamental.get("basis"))
            lines.append(
                f"- 目标区间：技术目标 {technical_text} {currency}；"
                f"基本面目标（{label}）{_fmt(fundamental.get('level'), 'fundamental target')} {currency}；两者分别标注。"
            )
        invalidation = trade_plan.get("invalidation_plan", {})
        if isinstance(invalidation, dict) and invalidation:
            technical = invalidation.get("technical_invalidation", {})
            lines.append(
                f"- 失效条件：技术失效 {_money(technical.get('level'), 'technical invalidation')} {currency}"
                f"（规则：{_text(technical.get('rule'), 'invalidation rule')}）；命题失效条件见第 8 章。"
            )
        if trade_plan.get("reward_risk_ratio") is not None:
            lines.append(f"- 收益风险比：{_money(trade_plan.get('reward_risk_ratio'), 'reward risk ratio')}。")
        if trade_plan.get("technical_valid_until"):
            lines.append(
                f"- 技术证据有效期至：{_as_of(trade_plan.get('technical_valid_until'), 'technical_valid_until')}；"
                f"价格数据 as_of：{_as_of(trade_plan.get('price_as_of'), 'price_as_of')}。"
            )
    else:
        lines.append(f"- 方案状态：{_text(status, 'trade-plan status')}。")
        lines.append("- 结论：不产生方案（决策门未全部通过，只输出观察/等待条件，不输出价格区间）。")
        veto = trade_plan.get("veto")
        if isinstance(veto, dict):
            gate = _statement(veto.get("gate"), "trade-plan veto gate")
            reason = _statement(veto.get("reason"), "trade-plan veto reason")
            lines.append(f"- 否决原因：{gate}——{reason}")
        elif veto:
            lines.append(f"- 否决原因：{_statement(veto, 'trade-plan veto')}。")
        entry_plan = trade_plan.get("entry_plan")
        if isinstance(entry_plan, dict):
            if entry_plan.get("reason"):
                lines.append(
                    f"- 未产出入场方案的原因：{_statement(entry_plan.get('reason'), 'entry-plan reason')}"
                )
            what_would_change = entry_plan.get("what_would_change", [])
            if isinstance(what_would_change, list) and what_would_change:
                lines.append("- 观察/等待条件：")
                lines.extend(
                    f"  - {_statement(item, 'what would change')}" for item in what_would_change
                )
            triggers = entry_plan.get("trigger_conditions", [])
            if isinstance(triggers, list) and triggers:
                lines.append("- 触发条件（条件式，全部满足才生效）：")
                lines.extend(f"  - {_statement(item, 'trigger condition')}" for item in triggers)
        watch = trade_plan.get("watch_conditions") or trade_plan.get("risk_triggers") or []
        if isinstance(watch, list) and watch:
            lines.append("- 风险跟踪条件：")
            lines.extend(f"  - {_statement(item, 'watch condition')}" for item in watch)
    lines.append("")
    return lines


def _thesis_lines(thesis: dict[str, Any]) -> list[str]:
    lines = ["### 反方论证与事前风险预演"]
    counter = thesis.get("counter_thesis")
    if counter:
        lines.append(f"- 反方论证：{_statement(counter, 'counter thesis')}")
    premortem = thesis.get("premortem")
    if premortem:
        lines.append(f"- 事前风险预演：{_statement(premortem, 'premortem')}")
    conditions = thesis.get("falsification_conditions", [])
    if isinstance(conditions, list) and conditions:
        lines.append("- 可证伪条件：")
        lines.extend(f"  - {_statement(item, 'falsification condition')}" for item in conditions)
    lines.append("")
    return lines


def _hypothesis_lines(thesis: dict[str, Any]) -> list[str]:
    lines = ["### 预注册命题与现金问题"]
    hypothesis = thesis.get("preregistered_hypothesis")
    if hypothesis:
        lines.append(f"- 预注册命题：{_statement(hypothesis, 'preregistered hypothesis')}")
    view = thesis.get("independent_view")
    if isinstance(view, dict) and view.get("statement"):
        lines.append(f"- 独立观点：{_statement(view.get('statement'), 'independent view')}")
    cash = thesis.get("cash_question")
    if isinstance(cash, dict) and cash.get("reason"):
        answer = "是" if cash.get("would_deploy") is True else "否"
        lines.append(
            f"- 现金问题（若今天这是一笔现金，是否按本方案部署）：{answer}——"
            f"{_statement(cash.get('reason'), 'cash question')}"
        )
    lines.append("")
    return lines


def _base_rate_lines(thesis: dict[str, Any]) -> list[str]:
    base_rate = thesis.get("base_rate")
    if not isinstance(base_rate, dict):
        return []
    lines = ["### 基准率比较"]
    percentile = base_rate.get("percentile")
    if percentile is not None:
        lines.append(f"- 基准率分位：{_fmt(percentile, 'base rate percentile')}。")
    if base_rate.get("exceed_reason"):
        lines.append(f"- 超越基准率的结构性理由：{_statement(base_rate.get('exceed_reason'), 'base rate exceed reason')}")
    metrics = base_rate.get("verification_metrics", [])
    if isinstance(metrics, list) and metrics:
        lines.append("- 验证指标：" + "、".join(_statement(item, "verification metric") for item in metrics))
    if base_rate.get("source"):
        lines.append(f"- 基准率来源：{_source(base_rate.get('source'), 'base rate', ALL_SOURCE_KINDS)}")
    lines.append("")
    return lines


def _expectation_gap_lines(expectation_gap: object) -> list[str]:
    if expectation_gap is None:
        return ["数据不可用：未提供预期差、催化剂与跟踪清单材料。", ""]
    if not isinstance(expectation_gap, dict):
        raise UnderwritingError("expectation_gap requires an object")
    lines: list[str] = []
    for key, title in (
        ("narrative", "预期差叙事"),
        ("catalysts", "催化剂"),
        ("tracking", "跟踪清单"),
    ):
        lines.append(f"### {title}")
        lines.extend(_evidence(title, expectation_gap.get(key)))
    return lines


def _source_registry(items: object) -> list[str]:
    if not isinstance(items, list) or not items:
        return ["- 数据不可用：未提供独立来源索引。"]
    return [f"- {_source(item, 'source registry', ALL_SOURCE_KINDS)}" for item in items]


def render_underwriting(fixture: dict[str, Any], base_dir: Path | None = None) -> str:
    if type(fixture.get("schema_version")) is not int or fixture.get("schema_version") != 1:
        raise UnderwritingError("underwriting inputs require schema_version 1")
    identity = _identity_record(
        fixture.get("identity"),
        "identity",
        ("issuer_id", "listing_id", "case_id"),
    )
    version = fixture.get("identity", {})
    artifact_version = version.get("artifact_version") if isinstance(version, dict) else None
    if type(artifact_version) is not int or artifact_version != 1:
        raise UnderwritingError("identity artifact_version must be 1")
    schema_version = version.get("schema_version") if isinstance(version, dict) else None
    if type(schema_version) is not int or schema_version != 1:
        raise UnderwritingError("identity schema_version must be 1")
    identity["artifact_version"] = str(artifact_version)
    case_id = identity["case_id"]
    symbol = _text(fixture.get("symbol"), "fixture symbol")
    mode = _text(fixture.get("mode"), "fixture mode")
    if mode not in MODES:
        raise UnderwritingError("mode must be initial or earnings_update")
    if symbol != identity["listing_id"]:
        raise UnderwritingError("fixture symbol must match identity listing_id")
    market_scope = _text(fixture.get("market_scope"), "fixture market_scope")
    currency = _text(fixture.get("currency"), "fixture currency")
    try:
        scopes = json.loads(MARKET_CONTRACTS.read_text(encoding="utf-8"))["scopes"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise UnderwritingError(f"market contracts are unavailable: {error}") from error
    _market_contract(scopes, market_scope, currency, identity["listing_id"])
    research_as_of, research_as_of_moment = _as_of_moment(
        fixture.get("research_as_of"), "fixture research_as_of"
    )
    _validate_source_times(fixture, research_as_of_moment)
    issuer, issuer_source = _identity(fixture.get("issuer_identity"), symbol)
    industry_entry = _industry_entry(fixture.get("industry"))
    sections = fixture.get("sections")
    if not isinstance(sections, dict):
        raise UnderwritingError("sections requires an object")
    valuation = _artifact(fixture.get("valuation"), "valuation", identity)
    earnings_quality = _artifact(fixture.get("earnings_quality"), "earnings_quality", identity)
    trade_plan = _artifact(fixture.get("trade_plan"), "trade_plan", identity)
    for name, artifact in (("valuation", valuation), ("earnings_quality", earnings_quality), ("trade_plan", trade_plan)):
        if artifact.get("market_scope") not in {None, market_scope}:
            raise UnderwritingError(f"{name} market_scope does not match underwriting market_scope")
        if artifact.get("currency") not in {None, currency}:
            raise UnderwritingError(f"{name} currency does not match underwriting currency")
    _validate_terminal_checks(valuation, trade_plan)
    _trade_plan_rules(trade_plan)
    _earnings_trade_gate(earnings_quality, trade_plan)
    # 嵌入工件的 computed_as_of 不得晚于 research_as_of：未来时间的工件 fail closed。
    for artifact_name, artifact in (
        ("valuation", valuation),
        ("earnings_quality", earnings_quality),
        ("trade_plan", trade_plan),
    ):
        _, artifact_moment = _as_of_moment(
            artifact.get("computed_as_of"), f"{artifact_name} computed_as_of"
        )
        if artifact_moment > research_as_of_moment:
            raise UnderwritingError(
                f"{artifact_name} computed_as_of is after research as_of"
            )
    thesis = fixture.get("thesis")
    if not isinstance(thesis, dict):
        raise UnderwritingError("thesis requires an object")
    evidence_ref = fixture.get("technical_evidence_ref")
    if not isinstance(evidence_ref, dict):
        raise UnderwritingError("technical_evidence_ref requires an object")
    evidence_id = _text(evidence_ref.get("evidence_id"), "technical_evidence_ref")
    evidence_as_of, evidence_as_of_moment = _as_of_moment(
        evidence_ref.get("as_of"), "technical_evidence_ref"
    )
    if evidence_as_of_moment > research_as_of_moment:
        raise UnderwritingError("technical_evidence_ref as_of is after research as_of")
    if base_dir is None:
        base_dir = Path.cwd()
    evidence_id, evidence_path = _technical_evidence(evidence_ref, identity, base_dir)
    # 技术证据引用必须携带完整身份，与报告身份逐项核对，无可选旁路。
    _verify_identity("technical_evidence_ref", evidence_ref.get("identity"), identity)
    assumptions = fixture.get("assumptions", [])
    gaps = fixture.get("data_gaps", [])
    if not isinstance(assumptions, list) or not isinstance(gaps, list):
        raise UnderwritingError("assumptions and data gaps must be lists")

    baseline_lines, baseline_gaps = _baseline_gaps(fixture.get("accounting_baseline"))

    lines = [
        f"# 深度研究：{symbol}",
        "",
        *_report_summary_lines(
            symbol,
            issuer,
            mode,
            currency,
            earnings_quality,
            trade_plan,
            valuation,
        ),
        f"- 身份：issuer_id={identity['issuer_id']}；listing_id={identity['listing_id']}；"
        f"case_id={case_id}；artifact_version={identity['artifact_version']}",
        f"- 模式：{MODES[mode]}（{mode}）",
        f"- 市场范围：{market_scope}；币种：{currency}",
        f"- 研究截至：{research_as_of}",
        f"- 发行人核验：{issuer['company_name']}（{issuer['ticker']}，{issuer['exchange']}；"
        f"发行人：{issuer['issuer']}），唯一核验通过（来源：{issuer_source}）",
        "",
    ]

    for number, (title, key) in enumerate(CHAPTERS, 1):
        lines.append(f"## {number}. {title}")
        if number == 1:
            if mode == "earnings_update":
                lines.append(
                    "本报告为财报更新模式：主线为变化（预期差收敛/扩大、分部与 KPI、"
                    "GAAP/Non-GAAP 桥接、现金流与营运资本、电话会、指引、模型与估值变动、对原方案的影响）。"
                )
                if fixture.get("prior_model_missing") is True or not fixture.get("prior_model"):
                    lines.append(
                        "输入标记 prior_model_missing=true：无旧模型，自动降级为首次承保，"
                        "以下各章按首次承保交付，不拒绝处理。"
                    )
                lines.append("")
            lines.extend(_evidence(title, sections.get(key)))
            lines.extend(_hypothesis_lines(thesis))
            lines.extend(_trade_conclusion_lines(trade_plan, currency))
        elif number == 3:
            lines.extend(_industry_lines(industry_entry))
            lines.extend(_evidence(title, sections.get(key)))
        elif number == 5:
            lines.extend(_evidence(title, sections.get(key)))
            lines.extend(baseline_lines)
            lines.extend(_earnings_quality_lines(earnings_quality))
            if baseline_gaps:
                lines.append("### 基线缺口")
                lines.extend(f"- {gap}" for gap in baseline_gaps)
                lines.append("")
        elif number == 6:
            lines.extend(_expectation_gap_lines(fixture.get("expectation_gap")))
            lines.extend(_base_rate_lines(thesis))
            lines.extend(_evidence(title, sections.get(key)))
        elif number == 7:
            lines.extend(_valuation_lines(valuation, currency))
            lines.extend(_evidence(title, sections.get(key)))
        elif number == 8:
            lines.extend(_thesis_lines(thesis))
            lines.extend(_evidence(title, sections.get(key)))
        elif number == 9:
            lines.extend(_evidence(title, sections.get(key)))
            lines.extend(["### 来源索引", *_source_registry(fixture.get("sources")), ""])
            lines.extend([
                "### 数据对账与时间戳",
                f"- 币种：{currency}；市场范围：{market_scope}。",
                f"- 估值 computed_as_of：{_as_of(valuation.get('computed_as_of'), 'valuation computed_as_of')}；"
                f"财报质量 computed_as_of：{_as_of(earnings_quality.get('computed_as_of'), 'earnings-quality computed_as_of')}；"
                f"交易方案 computed_as_of：{_as_of(trade_plan.get('computed_as_of'), 'trade-plan computed_as_of')}。",
                f"- 技术证据引用：evidence_id={evidence_id}；as_of={evidence_as_of}；artifact_path={evidence_path}"
                "（OHLCV 不内嵌，由技术面 skill 原子生成）。",
                "",
                "### 假设",
            ])
            if assumptions:
                lines.extend(f"- {_statement(item, 'assumption')}" for item in assumptions)
            else:
                lines.append("- 本次未记录额外假设。")
            lines.extend(["", "### 数据缺口"])
            combined_gaps = [_statement(item, "data gap") for item in gaps]
            combined_gaps.extend(baseline_gaps)
            for artifact_name, artifact in (
                ("估值", valuation),
                ("财报质量", earnings_quality),
                ("交易方案", trade_plan),
            ):
                artifact_gaps = artifact.get("data_gaps", [])
                if isinstance(artifact_gaps, list):
                    combined_gaps.extend(
                        f"{artifact_name}缺口：{_statement(item, f'{artifact_name} data gap')}"
                        for item in artifact_gaps
                    )
            if combined_gaps:
                lines.extend(f"- {gap}" for gap in combined_gaps)
            else:
                lines.append("- 本次未记录额外数据缺口。")
            lines.append("")
        else:
            lines.extend(_evidence(title, sections.get(key)))
    report = "\n".join(lines).rstrip() + "\n"
    if TRADE_DIRECTIVE.search(report):
        raise UnderwritingError("rendered report contains a trade directive")
    return report


# ---------------------------------------------------------------------------
# HTML reading view
# ---------------------------------------------------------------------------

COLLAPSIBLE_TITLES = {
    5: {"### 年度基线", "### 季度基线"},
    9: {"### 来源索引", "### 数据缺口"},
}

HTML_CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
       margin: 0 auto; max-width: 52rem; padding: 1.5rem; line-height: 1.65;
       color: #1c2430; background: #fafbfc; }
h1 { font-size: 1.6rem; border-bottom: 2px solid #d0d7de; padding-bottom: .4rem; }
h2 { font-size: 1.25rem; margin-top: 2rem; border-bottom: 1px solid #e2e8f0; padding-bottom: .25rem; }
h3 { font-size: 1.05rem; margin-top: 1.2rem; }
table { border-collapse: collapse; width: 100%; margin: .6rem 0; font-size: .92rem; }
th, td { border: 1px solid #d0d7de; padding: .3rem .55rem; text-align: left; }
th { background: #eef2f6; }
.cards { display: flex; flex-wrap: wrap; gap: .6rem; margin: 1rem 0; }
.card { border: 1px solid #d0d7de; border-radius: .5rem; background: #fff;
        padding: .55rem .8rem; min-width: 10rem; }
.card .card-title { font-size: .78rem; color: #57606a; }
.card .card-value { font-size: 1.05rem; font-weight: 600; }
nav.toc { background: #fff; border: 1px solid #d0d7de; border-radius: .5rem;
          padding: .6rem .9rem; font-size: .92rem; }
nav.toc ol { margin: .3rem 0; padding-left: 1.4rem; }
details { border: 1px solid #d0d7de; border-radius: .5rem; background: #fff;
          padding: .5rem .8rem; margin: .7rem 0; }
summary { font-weight: 600; cursor: pointer; }
.meta { color: #57606a; font-size: .9rem; }
""".strip()


def _md_inline(text: str) -> str:
    escaped = html_lib.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    # Render Markdown links as plain text with the URL kept visible; the
    # offline view must not carry external src=/href= references.
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", escaped)
    return escaped


def _is_table_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-+:?", cell.strip()) for cell in cells)


def _md_lines_to_html(lines: list[str], collapsible: set[str]) -> str:
    out: list[str] = []
    index = 0
    details_open = False

    def close_details() -> None:
        nonlocal details_open
        if details_open:
            out.append("</details>")
            details_open = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("## "):
            close_details()
            out.append(f"<h2>{_md_inline(stripped[3:].strip())}</h2>")
            index += 1
            continue
        if stripped.startswith("### "):
            close_details()
            title = stripped[4:].strip()
            if f"### {title}" in collapsible or stripped in collapsible:
                out.append(f"<details><summary>{html_lib.escape(title)}</summary>")
                details_open = True
            else:
                out.append(f"<h3>{_md_inline(title)}</h3>")
            index += 1
            continue
        if stripped.startswith("|"):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            rows = [
                [cell.strip() for cell in row.strip("|").split("|")] for row in table_lines
            ]
            out.append("<table>")
            body_rows = rows
            if rows:
                out.append("<thead><tr>" + "".join(f"<th>{_md_inline(cell)}</th>" for cell in rows[0]) + "</tr></thead>")
                body_rows = rows[1:]
            if body_rows and _is_table_separator(body_rows[0]):
                body_rows = body_rows[1:]
            out.append("<tbody>")
            for row in body_rows:
                out.append("<tr>" + "".join(f"<td>{_md_inline(cell)}</td>" for cell in row) + "</tr>")
            out.append("</tbody></table>")
            continue
        if stripped.startswith("- "):
            out.append("<ul>")
            while index < len(lines) and lines[index].strip().startswith("- "):
                out.append(f"<li>{_md_inline(lines[index].strip()[2:])}</li>")
                index += 1
            out.append("</ul>")
            continue
        if stripped.startswith("  - "):
            out.append(f"<ul><li>{_md_inline(stripped[4:])}</li></ul>")
            index += 1
            continue
        if not stripped:
            index += 1
            continue
        out.append(f"<p>{_md_inline(stripped)}</p>")
        index += 1
    close_details()
    return "\n".join(out)


def _watch_valuation_cards(
    valuation: dict[str, Any], currency: str
) -> list[tuple[str, str]]:
    """Valuation reference cards for a watch trade plan.

    A usable driver-based DCF takes precedence over a usable PE model. If the
    DCF is only conditional, a usable PE model is still shown; this keeps the
    display order aligned with the trade-plan value-source resolver. Anything
    else — including the legacy baseline DCF whose cash-flow path is
    hand-supplied rather than driver-derived — is shown as 基本面目标 未形成 /
    估值模型状态 待重建, never as a 基本面估值锚.
    Reads only values already computed in the valuation artifact and shows
    every finite value verbatim — the renderer adds no positivity or ordering
    rules of its own; only missing or non-finite values fail closed to
    未计算. When no quality-gated DCF/PE model applies, the first computed
    EPV/EVA/SOTP point estimate keeps the legacy non-actionable anchor card."""
    results = valuation.get("results")
    results = results if isinstance(results, dict) else {}
    deferred_cards: list[tuple[str, str]] | None = None
    driver = results.get("driver_dcf")
    if isinstance(driver, dict) and driver.get("status") == "computed":
        quality_status = _driver_dcf_quality(driver)
        weighted = _finite_number(driver.get("probability_weighted_per_share"))
        zone = driver.get("value_zone")
        low = _finite_number(zone.get("low")) if isinstance(zone, dict) else None
        high = _finite_number(zone.get("high")) if isinstance(zone, dict) else None
        zone_text = (
            f"{_fmt(low)} – {_fmt(high)} {currency}"
            if low is not None and high is not None
            else "未计算"
        )
        if quality_status == "usable":
            return [
                (
                    "定制 DCF 参考值",
                    f"{_fmt(weighted)} {currency}" if weighted is not None else "未计算",
                ),
                ("定制 DCF 参考区间", zone_text),
            ]
        if quality_status == "conditional":
            deferred_cards = [
                (
                    "条件性模型输出",
                    f"{_fmt(weighted)} {currency}" if weighted is not None else "未计算",
                ),
                ("估值参考区间", zone_text),
            ]
        else:
            deferred_cards = [("基本面目标", "未形成"), ("估值模型状态", "待重建")]
    pe = results.get("pe")
    if isinstance(pe, dict) and pe.get("status") == "computed":
        quality_status = _pe_quality_status(pe)
        weighted = _finite_number(pe.get("probability_weighted_per_share"))
        zone = pe.get("value_zone")
        low = _finite_number(zone.get("low")) if isinstance(zone, dict) else None
        high = _finite_number(zone.get("high")) if isinstance(zone, dict) else None
        zone_text = (
            f"{_fmt(low)} – {_fmt(high)} {currency}"
            if low is not None and high is not None
            else "未计算"
        )
        if quality_status == "usable":
            return [
                (
                    "PE 参考值",
                    f"{_fmt(weighted)} {currency}" if weighted is not None else "未计算",
                ),
                ("PE 参考区间", zone_text),
            ]
        if quality_status == "conditional":
            if deferred_cards is None:
                deferred_cards = [
                    (
                        "条件性 PE 输出",
                        f"{_fmt(weighted)} {currency}" if weighted is not None else "未计算",
                    ),
                    ("估值参考区间", zone_text),
                ]
        elif deferred_cards is None:
            deferred_cards = [("基本面目标", "未形成"), ("估值模型状态", "待重建")]
    if deferred_cards is not None:
        return deferred_cards
    dcf = results.get("dcf")
    if isinstance(dcf, dict) and dcf.get("status") == "computed":
        return [
            ("基本面目标", "未形成"),
            ("估值模型状态", "待重建（baseline 未由驱动推导）"),
        ]
    for model, key in (
        ("epv", "epv_per_share"),
        ("eva", "residual_income_per_share"),
        ("sotp", "per_share"),
    ):
        entry = results.get(model)
        if not isinstance(entry, dict) or entry.get("status") != "computed":
            continue
        anchor = _finite_number(entry.get(key))
        if anchor is None:
            continue
        return [
            ("基本面估值锚", f"{_fmt(anchor)} {currency}"),
            ("估值参考区间", "未计算"),
        ]
    return [("基本面目标", "未形成"), ("估值模型状态", "待重建")]


def _summary_number(value: object) -> str | None:
    number = _finite_number(value)
    if number is None:
        return None
    text = f"{number:,.2f}".rstrip("0").rstrip(".")
    return text


def _summary_money(value: object, currency: str) -> str:
    text = _summary_number(value)
    return f"{text} {currency}" if text is not None else "未计算"


def _summary_zone(zone: object, currency: str) -> str:
    if not isinstance(zone, dict):
        return "未计算"
    low = _finite_number(zone.get("low"))
    high = _finite_number(zone.get("high"))
    if low is None or high is None:
        return "未计算"
    return f"{_summary_number(low)}–{_summary_number(high)} {currency}"


def _valuation_reference_summary(
    valuation: dict[str, Any], currency: str
) -> str:
    results = valuation.get("results")
    if not isinstance(results, dict):
        return "未计算"
    references: list[str] = []
    driver = results.get("driver_dcf")
    if isinstance(driver, dict) and driver.get("status") == "computed":
        quality = _driver_dcf_quality(driver)
        zone_text = _summary_zone(driver.get("value_zone"), currency)
        references.append(
            f"驱动型 DCF {_summary_money(driver.get('probability_weighted_per_share'), currency)}"
            f"（区间 {zone_text}，{quality or 'unknown'}）"
        )
    pe = results.get("pe")
    if isinstance(pe, dict):
        if pe.get("status") == "computed":
            quality = _pe_quality_status(pe)
            zone_text = _summary_zone(pe.get("value_zone"), currency)
            references.append(
                f"PE {_summary_money(pe.get('probability_weighted_per_share'), currency)}"
                f"（区间 {zone_text}，{quality or 'unknown'}）"
            )
        elif pe.get("status") == "missing_inputs":
            references.append("PE 未计算（缺少前瞻/正常化 EPS 或情景倍数）")
        elif pe.get("status") == "not_applicable":
            references.append("PE 不适用")
    else:
        references.append("PE 未请求")
    dcf = results.get("dcf")
    if isinstance(dcf, dict) and dcf.get("status") == "computed":
        zone_text = _summary_zone(dcf.get("value_zone"), currency)
        references.append(
            f"DCF 基线 {_summary_money(dcf.get('probability_weighted_per_share'), currency)}"
            f"（区间 {zone_text}，仅留档）"
        )
    return "；".join(references) if references else "未计算"


def _report_summary_lines(
    symbol: str,
    issuer: dict[str, str],
    mode: str,
    currency: str,
    earnings_quality: dict[str, Any],
    trade_plan: dict[str, Any],
    valuation: dict[str, Any],
) -> list[str]:
    """Make the deliverable status and decision cards visible in Markdown too."""
    target_plan = trade_plan.get("target_plan")
    fundamental = target_plan.get("fundamental_target") if isinstance(target_plan, dict) else None
    fundamental_level = fundamental.get("level") if isinstance(fundamental, dict) else None
    if fundamental_level is not None:
        fundamental_text = _summary_money(fundamental_level, currency)
        if isinstance(fundamental, dict) and fundamental.get("basis"):
            fundamental_text += f"（{_fundamental_target_label(fundamental.get('basis'))}）"
    else:
        fundamental_text = "未计算（未形成可用基本面目标）"
    entry_plan = trade_plan.get("entry_plan")
    value_band = entry_plan.get("value_band") if isinstance(entry_plan, dict) else None
    if isinstance(value_band, dict) and value_band.get("low") is not None and value_band.get("high") is not None:
        value_zone_text = (
            f"{_summary_money(value_band.get('low'), currency)}–"
            f"{_summary_money(value_band.get('high'), currency)}"
        )
    else:
        value_zone_text = "未计算（watch 不输出交易价值带）"
    grade = _text(earnings_quality.get("grade"), "earnings-quality grade")
    status = _text(trade_plan.get("status"), "trade-plan status")
    return [
        "## 报告摘要",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| 报告名称 | 深度研究：{issuer['company_name']}（{symbol}） |",
        "| 产出状态 | 已完成 |",
        f"| 研究模式 | {MODES[mode]} |",
        f"| 财报质量级别 | **{grade}** |",
        f"| 基本面目标 | **{fundamental_text}** |",
        f"| 价值区间 | **{value_zone_text}** |",
        f"| 方案状态 | **{status}** |",
        f"| 估值参考 | {_valuation_reference_summary(valuation, currency)} |",
        "",
    ]


def render_html(fixture: dict[str, Any], markdown: str) -> str:
    """Build the single-file offline reading view from the rendered Markdown."""
    symbol = _text(fixture.get("symbol"), "fixture symbol")
    currency = _text(fixture.get("currency"), "fixture currency")
    earnings_quality = fixture["earnings_quality"]
    trade_plan = fixture["trade_plan"]
    grade = _text(earnings_quality.get("grade"), "earnings-quality grade")
    provisional = earnings_quality.get("provisional") is True
    status = _text(trade_plan.get("status"), "trade-plan status")
    target_plan = trade_plan.get("target_plan")
    fundamental = (
        target_plan.get("fundamental_target") if isinstance(target_plan, dict) else None
    )
    if not isinstance(fundamental, dict):
        fundamental = {}
    fundamental_level = fundamental.get("level")
    fundamental_label = (
        _fundamental_target_label(fundamental.get("basis"))
        if fundamental_level is not None
        else "基本面目标"
    )
    entry_plan = trade_plan.get("entry_plan")
    value_band = entry_plan.get("value_band") if isinstance(entry_plan, dict) else None

    if status == "watch":
        # watch 不产出可执行价位；摘要卡仅展示估值 artifact 已计算的
        # 非行动性参考值（估值锚 / DCF 估值参考区间），无有效估值则未计算。
        valuation_cards = _watch_valuation_cards(fixture["valuation"], currency)
    else:
        valuation_cards = [
            (
                fundamental_label,
                f"{_fmt(fundamental_level)} {currency}"
                if fundamental_level is not None
                else "未计算",
            ),
            (
                "价值区间",
                f"{_fmt(value_band.get('low'))} – {_fmt(value_band.get('high'))} {currency}"
                if isinstance(value_band, dict) and value_band.get("low") is not None
                else "未计算",
            ),
        ]

    cards = [
        ("财报质量级别", f"{grade}{'（暂定）' if provisional else ''}"),
        *valuation_cards,
        ("方案状态", status),
    ]
    card_html = "".join(
        f'<div class="card"><div class="card-title">{html_lib.escape(title)}</div>'
        f'<div class="card-value">{html_lib.escape(value)}</div></div>'
        for title, value in cards
    )

    # Split the rendered Markdown into header block and nine chapter bodies.
    chapter_pattern = re.compile(r"^## (\d)\. ", re.MULTILINE)
    matches = list(chapter_pattern.finditer(markdown))
    if len(matches) != 9:
        raise UnderwritingError("rendered Markdown does not contain nine chapters")
    header_markdown = markdown[: matches[0].start()].splitlines()
    chapter_bodies: list[list[str]] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(markdown)
        body = markdown[match.end():end].splitlines()
        # Drop the chapter title text (kept in the heading itself).
        chapter_bodies.append(body[1:] if body else [])

    toc_items = "".join(
        f'<li><a href="#chapter-{number}">{number}. {html_lib.escape(title)}</a></li>'
        for number, (title, _) in enumerate(CHAPTERS, 1)
    )
    header_html = _md_lines_to_html(
        [line for line in header_markdown if not line.startswith("# ")], set()
    )
    sections_html: list[str] = []
    for number, ((title, _), body) in enumerate(zip(CHAPTERS, chapter_bodies), 1):
        body_html = _md_lines_to_html(body, COLLAPSIBLE_TITLES.get(number, set()))
        sections_html.append(
            f'<section id="chapter-{number}"><h2>{number}. {html_lib.escape(title)}</h2>\n{body_html}\n</section>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>深度研究报告：{html_lib.escape(symbol)}</title>
<style>
{HTML_CSS}
</style>
</head>
<body>
<h1>深度研究报告：{html_lib.escape(symbol)}</h1>
<div class="cards">{card_html}</div>
{header_html}
<nav class="toc"><strong>目录</strong><ol>{toc_items}</ol></nav>
{chr(10).join(sections_html)}
</body>
</html>
"""


def _write_new(path: Path, content: str) -> None:
    resolved = path.resolve()
    if RUNTIME_ROOT == resolved or RUNTIME_ROOT in resolved.parents:
        raise UnderwritingError("output path must not be inside the Skill runtime package")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def _assert_output_path_allowed(path: Path) -> None:
    """Reject runtime-package destinations before reading or rendering input."""
    resolved = path.resolve()
    if RUNTIME_ROOT == resolved or RUNTIME_ROOT in resolved.parents:
        raise UnderwritingError("output path must not be inside the Skill runtime package")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--html", type=Path)
    arguments = parser.parse_args()
    try:
        _assert_output_path_allowed(arguments.output)
        if arguments.html is not None:
            _assert_output_path_allowed(arguments.html)
        fixture = json.loads(arguments.input.read_text(encoding="utf-8"))
        if not isinstance(fixture, dict):
            raise UnderwritingError("underwriting inputs must be a JSON object")
        markdown = render_underwriting(fixture, arguments.input.parent)
        html_view = render_html(fixture, markdown) if arguments.html else None
        _write_new(arguments.output, markdown)
        if html_view is not None:
            _write_new(arguments.html, html_view)
    except (OSError, json.JSONDecodeError, UnderwritingError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
