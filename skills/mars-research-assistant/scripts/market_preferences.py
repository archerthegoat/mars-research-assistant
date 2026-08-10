#!/usr/bin/env python3
"""Market-scope preferences and resolution for Mars Research Assistant v1.0.3.

Manages the user-visible preference file ``mars-market-preferences.json`` in the
active workspace and resolves a user query (ticker, bare code, or name) to a
market scope per the v1.0.3 implementation contract section 2:

- explicit exchange suffix always wins (``.HK`` -> hk; ``.SS/.SH/.SZ`` ->
  a_share; ``.US`` -> us), regardless of how many scopes are enabled;
- a bare alphabetic ticker — any single 1-5 letter token, case-insensitive
  (e.g. ``lite``/``LITE``) — is always classified as a ticker candidate,
  never as a mode name; it resolves to ``us`` only when ``us`` is the sole
  enabled base scope (``--once-scope us`` counts as enabling us); whenever
  ``us`` is not the sole enabled base scope — hk only, a_share only,
  hk+a_share, or any multi-scope combination — it yields ``ambiguous`` with
  ``needs_user_selection`` and ``query_kind: ticker``, asking only for a
  market/exchange selection and never for a company name first; it is never
  guessed locally;
- bare numeric codes: 6-digit starting with 6 -> SSE, 6-digit starting with
  0/3 -> SZSE, 1-5 digits -> HKEX;
- a resolved scope outside ``enabled_market_scopes`` yields ``out_of_scope``
  with the explicit options ``once`` / ``add_to_scope`` (never auto-enabled);
- a missing preference file, or one whose ``enabled_market_scopes`` is
  missing or empty, yields ``onboarding_required`` (never defaults to US
  equities) with ``multi_select: true`` and all four scopes (美股 / 港股 /
  A 股 / A/H 对比) as simultaneously selectable options — the copy must never
  imply a single choice; a bare alphabetic ticker query also carries a
  ``ticker_hint`` quick-confirm (e.g. "美股时可确认 LITE（NASDAQ）");
  the file is only read when ``schema_version`` is exactly 1 —
  any other version is rejected;
- ``ah_compare`` implies the ``hk`` and ``a_share`` base scopes at resolution
  time too, so a bare alphabetic ticker with ``ah_compare`` enabled is
  ``ambiguous`` rather than silently resolved;
- Chinese / free-text names are never guessed locally: ``ambiguous`` with
  ``needs_user_selection`` so the caller must ask the user;
- ``--once-scope`` applies to the current resolution only and is never
  persisted;
- ``--ah-pair`` validates an A/H listing pair (one a_share + one hk listing,
  CNY/HKD currency pair) and emits the mandatory disclosure checklist;
  exchange suffixes only prove market/currency, never issuer identity, so
  a format-only pair yields ``needs_user_selection`` and ``pair_status``
  ``ok`` additionally requires ``--ah-issuer-id`` asserting one shared
  issuer (two comma-separated ids that differ fail the pair).

Standard library only. JSON is printed to stdout with
``ensure_ascii=False, indent=2``.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any


class MarketPreferenceError(ValueError):
    """Report an invalid preference or query without guessing."""


SCHEMA_VERSION = 1
PREFERENCES_FILENAME = "mars-market-preferences.json"
VALID_SCOPES = ("us", "hk", "a_share", "ah_compare")
BASE_SCOPES = ("us", "hk", "a_share")
AH_COMPARE_IMPLIES = ("hk", "a_share")

SUFFIX_SCOPES = {
    ".HK": "hk",
    ".SS": "a_share",
    ".SH": "a_share",
    ".SZ": "a_share",
    ".US": "us",
}
SCOPE_CURRENCY = {"us": "USD", "hk": "HKD", "a_share": "CNY"}
SCOPE_EXCHANGE = {"us": "NYSE/NASDAQ", "hk": "HKEX", "a_share": "SSE/SZSE"}
SCOPE_LABELS = {"us": "美股", "hk": "港股", "a_share": "A 股", "ah_compare": "A/H 对比"}

CJK_PATTERN = re.compile(r"[一-鿿]")
TICKER_WITH_SUFFIX = re.compile(r"[A-Z0-9]{1,10}\.[A-Z]{1,3}")
BARE_ALPHA_TICKER = re.compile(r"[A-Za-z]{1,5}")
BARE_NUMERIC_CODE = re.compile(r"\d{1,6}")

DEFAULT_AH_MUST_REPORT = [
    "fx_rate",
    "share_right_ratio",
    "liquidity_diff",
    "trading_day_diff",
    "premium_discount",
]
PREFERENCE_NOTE = "每台设备本地保存；只有用户明确导入/同步时才跟随 Drive。"

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
MARKET_CONTRACTS_PATH = (
    RUNTIME_ROOT
    / "skills"
    / "deep-equity-research"
    / "reference"
    / "market_contracts.json"
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _preferences_path(workspace: Path) -> Path:
    return workspace / PREFERENCES_FILENAME


def _load_preferences(workspace: Path) -> dict[str, Any] | None:
    path = _preferences_path(workspace)
    if path.is_symlink():
        raise MarketPreferenceError("preference file must not be a symlink")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MarketPreferenceError(
            f"preference file {path} is unreadable: {error}"
        ) from error
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data.get("schema_version") != SCHEMA_VERSION
    ):
        raise MarketPreferenceError(
            f"preference file {path} does not match schema_version 1"
        )
    scopes = data.get("enabled_market_scopes")
    if scopes is None:
        # 缺省等同于未配置：show/resolve 上报 onboarding_required。
        data["enabled_market_scopes"] = []
        return data
    if not isinstance(scopes, list):
        raise MarketPreferenceError(
            f"preference file {path} does not match schema_version 1"
        )
    unknown = sorted({str(scope) for scope in scopes if scope not in VALID_SCOPES})
    if unknown:
        raise MarketPreferenceError(
            f"preference file {path} lists unknown scopes: {unknown}"
        )
    return data


def _canonical_order(scopes: list[str]) -> list[str]:
    return [scope for scope in VALID_SCOPES if scope in set(scopes)]


def _expand_ah_compare(scopes: list[str]) -> tuple[list[str], list[str]]:
    expanded = list(scopes)
    added: list[str] = []
    if "ah_compare" in expanded:
        for implied in AH_COMPARE_IMPLIES:
            if implied not in expanded:
                expanded.append(implied)
                added.append(implied)
    return _canonical_order(expanded), added


def _load_ah_must_report() -> list[str]:
    try:
        contracts = json.loads(MARKET_CONTRACTS_PATH.read_text(encoding="utf-8"))
        must_report = contracts["scopes"]["ah_compare"]["must_report"]
        if isinstance(must_report, list) and must_report:
            return [str(item) for item in must_report]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return list(DEFAULT_AH_MUST_REPORT)


def _classify_query(query: str) -> dict[str, Any]:
    """Classify a raw query into ticker / bare code / name with a base scope."""
    text = query.strip()
    if not text:
        raise MarketPreferenceError("query must not be empty")
    if CJK_PATTERN.search(text) or " " in text:
        return {"kind": "name", "scope": None, "symbol": text, "reason": "name_query"}
    upper = text.upper()
    if TICKER_WITH_SUFFIX.fullmatch(upper):
        suffix = "." + upper.rsplit(".", 1)[1]
        scope = SUFFIX_SCOPES.get(suffix)
        if scope is None:
            raise MarketPreferenceError(
                f"unsupported exchange suffix '{suffix}' in query '{text}'"
            )
        return {
            "kind": "ticker",
            "scope": scope,
            "symbol": upper,
            "reason": "explicit_suffix",
        }
    if BARE_NUMERIC_CODE.fullmatch(text):
        if len(text) == 6:
            if text.startswith("6"):
                symbol, exchange = f"{text}.SS", "SSE"
            elif text[0] in "03":
                symbol, exchange = f"{text}.SZ", "SZSE"
            else:
                raise MarketPreferenceError(
                    f"unsupported 6-digit bare code '{text}'"
                )
            return {
                "kind": "code",
                "scope": "a_share",
                "symbol": symbol,
                "exchange": exchange,
                "reason": "bare_code",
            }
        symbol = text.zfill(4) if len(text) <= 4 else text
        return {
            "kind": "code",
            "scope": "hk",
            "symbol": f"{symbol}.HK",
            "exchange": "HKEX",
            "reason": "bare_code",
        }
    if BARE_ALPHA_TICKER.fullmatch(text):
        return {
            "kind": "ticker",
            "scope": "us",
            "symbol": upper,
            "reason": "bare_alpha_ticker",
        }
    return {"kind": "name", "scope": None, "symbol": text, "reason": "name_query"}


def _listing_candidate(classified: dict[str, Any]) -> dict[str, Any]:
    scope = classified["scope"]
    return {
        "symbol": classified["symbol"],
        "market_scope": scope,
        "exchange": classified.get("exchange", SCOPE_EXCHANGE[scope]),
        "currency": SCOPE_CURRENCY[scope],
    }


def _onboarding_payload(ticker_symbol: str | None = None) -> dict[str, Any]:
    """Onboarding response: markets are multi-select, never single-choice.

    A bare alphabetic ticker additionally carries a quick-confirm hint so the
    user can pick 美股 and confirm e.g. LITE（NASDAQ）in one step."""
    payload: dict[str, Any] = {
        "status": "onboarding_required",
        "multi_select": True,
        "options": [
            {"market_scope": scope, "label": SCOPE_LABELS[scope]}
            for scope in VALID_SCOPES
        ],
        "detail": "尚未配置市场偏好；市场可多选：美股、港股、A 股、A/H 对比可一次多选，不是单选。",
    }
    if ticker_symbol is not None:
        payload["ticker_hint"] = f"美股时可确认 {ticker_symbol}（NASDAQ）。"
    return payload


def _cmd_show(workspace: Path) -> int:
    preferences = _load_preferences(workspace)
    if preferences is None or not preferences["enabled_market_scopes"]:
        _print_json(_onboarding_payload())
        return 0
    _print_json({"status": "ok", "preferences": preferences})
    return 0


def _cmd_set(workspace: Path, scopes_arg: str, default_scope: str | None) -> int:
    scopes = [item.strip() for item in scopes_arg.split(",") if item.strip()]
    if not scopes:
        raise MarketPreferenceError("--scopes must list at least one market scope")
    unknown = [scope for scope in scopes if scope not in VALID_SCOPES]
    if unknown:
        raise MarketPreferenceError(
            f"unknown market scopes {unknown}; valid: {list(VALID_SCOPES)}"
        )
    expanded, auto_added = _expand_ah_compare(scopes)
    if default_scope is not None and default_scope not in expanded:
        raise MarketPreferenceError(
            f"--default '{default_scope}' must be one of the enabled scopes {expanded}"
        )
    preferences = {
        "schema_version": SCHEMA_VERSION,
        "enabled_market_scopes": expanded,
        "default_market_scope": default_scope,
        "updated_as_of": _now_iso(),
        "note": PREFERENCE_NOTE,
    }
    path = _preferences_path(workspace)
    if path.is_symlink():
        raise MarketPreferenceError("preference file must not be a symlink")
    path.write_text(
        json.dumps(preferences, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result: dict[str, Any] = {"status": "saved", "preferences": preferences}
    if auto_added:
        result["ah_compare_auto_expanded"] = auto_added
        result["expansion_note"] = (
            "选择 ah_compare 自动补齐 hk 与 a_share 基础范围并保留 ah_compare。"
        )
    _print_json(result)
    return 0


def _cmd_resolve(
    workspace: Path,
    query: str | None,
    ah_pair: str | None,
    ah_issuer_id: str | None,
    once_scope: str | None,
) -> int:
    if ah_pair is not None:
        return _cmd_resolve_ah_pair(ah_pair, ah_issuer_id)
    if ah_issuer_id is not None:
        raise MarketPreferenceError("--ah-issuer-id requires --ah-pair")
    if query is None:
        raise MarketPreferenceError("resolve requires --query or --ah-pair")
    preferences = _load_preferences(workspace)
    if preferences is None or not preferences["enabled_market_scopes"]:
        ticker_symbol = None
        try:
            classified = _classify_query(query)
        except MarketPreferenceError:
            classified = None
        if classified is not None and classified["reason"] == "bare_alpha_ticker":
            ticker_symbol = classified["symbol"]
        _print_json(_onboarding_payload(ticker_symbol))
        return 0
    enabled = list(preferences["enabled_market_scopes"])
    # ah_compare 蕴含 hk 与 a_share 基础范围，解析时同样生效，
    # 避免裸字母 ticker 在多个已启用基础范围下被静默归为 us。
    preference_enabled, _ = _expand_ah_compare(enabled)

    classified = _classify_query(query)
    if classified["kind"] == "name":
        _print_json(
            {
                "status": "ambiguous",
                "reason": "name_unresolvable_locally",
                "needs_user_selection": True,
                "query": classified["symbol"],
                "candidates": [{"market_scope": scope} for scope in enabled],
                "detail": "中文/名称查询不做本地猜测，必须由用户在已启用范围中选择。",
            }
        )
        return 0

    base_scope = classified["scope"]
    effective_enabled = set(preference_enabled)
    if once_scope is not None:
        if once_scope not in VALID_SCOPES:
            raise MarketPreferenceError(
                f"unknown --once-scope '{once_scope}'; valid: {list(VALID_SCOPES)}"
            )
        effective_enabled.add(once_scope)
        if once_scope == "ah_compare":
            effective_enabled.update(AH_COMPARE_IMPLIES)

    if classified["reason"] == "bare_alpha_ticker":
        base_scopes = {scope for scope in effective_enabled if scope in BASE_SCOPES}
        if base_scopes != {"us"}:
            # 裸字母 ticker 只有在美股是唯一已启用基础范围时才解析为美股；
            # 仅港股、仅 A 股、港股+A 股或任何多基础范围情形都必须询问，
            # 绝不退回 out_of_scope/us。
            _print_json(
                {
                    "status": "ambiguous",
                    "reason": "bare_alpha_multiple_scopes",
                    "needs_user_selection": True,
                    "query_kind": "ticker",
                    "query": classified["symbol"],
                    "candidates": [
                        {"market_scope": scope}
                        for scope in _canonical_order(list(effective_enabled))
                    ],
                    "detail": "已识别为 ticker 候选，但美股不是唯一已启用基础市场范围，不做本地猜测，需要用户选择市场/交易所（可使用交易所后缀，如 AAPL.US）；不要求先提供公司名。",
                }
            )
            return 0

    if base_scope not in effective_enabled:
        _print_json(
            {
                "status": "out_of_scope",
                "market_scope": base_scope,
                "query_symbol": classified["symbol"],
                "options": ["once", "add_to_scope"],
                "detail": "该市场未启用；不自动启用，需用户明确选择仅本次使用或加入范围。",
            }
        )
        return 0

    result: dict[str, Any] = {
        "status": "resolved",
        "market_scope": base_scope,
        "listing_candidates": [_listing_candidate(classified)],
        "reason": classified["reason"],
    }
    if base_scope in preference_enabled:
        result["enabled_via"] = "preferences"
    else:
        result["enabled_via"] = "once_scope"
        result["persisted"] = False
    _print_json(result)
    return 0


def _cmd_resolve_ah_pair(ah_pair: str, ah_issuer_id: str | None) -> int:
    parts = [item.strip() for item in ah_pair.split(",") if item.strip()]
    if len(parts) != 2:
        raise MarketPreferenceError(
            "--ah-pair requires exactly two listings, e.g. 600519.SS,0700.HK"
        )
    classified = [_classify_query(part) for part in parts]
    if any(item["kind"] == "name" for item in classified):
        _print_json(
            {
                "pair_status": "failed",
                "reason": "名称查询无法本地唯一解析，请提供带交易所后缀的 listing。",
            }
        )
        return 0
    scopes = [item["scope"] for item in classified]
    if sorted(scopes) != ["a_share", "hk"]:
        scope_desc = ",".join(scopes)
        _print_pair_failure(
            f"两个 listing 的市场分别为 {scope_desc}，必须一个 a_share 一个 hk，无法配对。"
        )
        return 0
    a_share, hk = (
        classified[scopes.index("a_share")],
        classified[scopes.index("hk")],
    )
    currencies = {SCOPE_CURRENCY["a_share"], SCOPE_CURRENCY["hk"]}
    if currencies != {"CNY", "HKD"}:
        _print_pair_failure("币种对不是 CNY/HKD，无法配对。")
        return 0
    if ah_issuer_id is None:
        _print_json(
            {
                "pair_status": "needs_user_selection",
                "reason": "交易所后缀只能验证市场与币种，无法本地确认两个 listing 属于同一发行人；请通过 --ah-issuer-id 显式声明共同发行人后再配对。",
                "a_share": _listing_candidate(a_share),
                "hk": _listing_candidate(hk),
                "fx_pair": "CNY/HKD",
                "must_report": _load_ah_must_report(),
            }
        )
        return 0
    issuer_ids = [item.strip() for item in ah_issuer_id.split(",") if item.strip()]
    if not issuer_ids or len(issuer_ids) > 2:
        raise MarketPreferenceError(
            "--ah-issuer-id accepts one shared issuer id or two comma-separated ids"
        )
    if len(issuer_ids) == 2 and issuer_ids[0] != issuer_ids[1]:
        _print_pair_failure(
            f"声明的发行人身份不一致（{issuer_ids[0]} vs {issuer_ids[1]}），无法唯一配对。"
        )
        return 0
    _print_json(
        {
            "pair_status": "ok",
            "a_share": _listing_candidate(a_share),
            "hk": _listing_candidate(hk),
            "fx_pair": "CNY/HKD",
            "must_report": _load_ah_must_report(),
            "asserted_issuer_id": issuer_ids[0],
            "identity_note": "发行人身份来自调用方通过 --ah-issuer-id 的显式声明，未做本地核验；A/H 对比共用一个 case_id、两个 listing_id。",
        }
    )
    return 0


def _print_pair_failure(reason: str) -> None:
    _print_json(
        {
            "pair_status": "failed",
            "reason": reason,
            "must_report": _load_ah_must_report(),
        }
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mars Research Assistant 市场偏好与 scope 解析（v1.0.3）。"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="活动工作目录（偏好文件所在处），默认当前目录。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("show", help="打印当前偏好；未配置时输出 onboarding_required。")

    set_parser = subparsers.add_parser("set", help="写入/更新市场偏好（可多选）。")
    set_parser.add_argument(
        "--scopes",
        required=True,
        help="逗号分隔的市场范围，取值 us,hk,a_share,ah_compare。",
    )
    set_parser.add_argument(
        "--default",
        dest="default_scope",
        default=None,
        help="可选默认市场范围，仅用于未指定市场的宽泛请求。",
    )

    resolve_parser = subparsers.add_parser("resolve", help="解析查询或 A/H 配对的市场范围。")
    resolve_group = resolve_parser.add_mutually_exclusive_group()
    resolve_group.add_argument("--query", default=None, help="ticker、裸代码或名称。")
    resolve_group.add_argument(
        "--ah-pair",
        default=None,
        help="两个 listing，逗号分隔，如 600519.SS,0700.HK。",
    )
    resolve_parser.add_argument(
        "--ah-issuer-id",
        default=None,
        help="仅配合 --ah-pair：显式声明两个 listing 的共同发行人 id/名称"
        "（或两个逗号分隔的 id，不一致则配对失败），声明会记录在输出中。",
    )
    resolve_parser.add_argument(
        "--once-scope",
        default=None,
        help="仅本次生效的市场范围，不写盘。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    workspace = args.workspace.resolve()
    try:
        if args.command == "show":
            return _cmd_show(workspace)
        if args.command == "set":
            return _cmd_set(workspace, args.scopes, args.default_scope)
        if args.command == "resolve":
            return _cmd_resolve(
                workspace, args.query, args.ah_pair, args.ah_issuer_id, args.once_scope
            )
    except MarketPreferenceError as error:
        print(str(error), file=sys.stderr)
        return 2
    raise MarketPreferenceError(f"unknown command '{args.command}'")


if __name__ == "__main__":
    sys.exit(main())
