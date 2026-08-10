"""Tests for v1.0.3 market-scope preferences and provider resolution."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MARKET_PREFERENCES = (
    ROOT / "skills" / "mars-research-assistant" / "scripts" / "market_preferences.py"
)
PROVIDERS = (
    ROOT
    / "skills"
    / "mars-research-assistant"
    / "skills"
    / "deep-equity-research"
    / "scripts"
    / "providers.py"
)
PREFERENCES_FILENAME = "mars-market-preferences.json"


def _load_providers_module():
    spec = importlib.util.spec_from_file_location("mars_providers", PROVIDERS)
    module = importlib.util.module_from_spec(spec)
    # Never write bytecode cache into the runtime package (verify_mars_skills
    # rejects development artifacts under skills/mars-research-assistant/).
    dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


providers = _load_providers_module()


class MarketPreferencesCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MARKET_PREFERENCES), "--workspace", str(self.workspace), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_ok(self, *args: str) -> dict:
        result = self._run(*args)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout)

    def _set_scopes(self, scopes: str, default: str | None = None) -> dict:
        args = ["set", "--scopes", scopes]
        if default is not None:
            args += ["--default", default]
        return self._run_ok(*args)

    def _read_preferences_file(self) -> dict:
        return json.loads(
            (self.workspace / PREFERENCES_FILENAME).read_text(encoding="utf-8")
        )

    def _write_preferences(self, preferences: dict) -> None:
        (self.workspace / PREFERENCES_FILENAME).write_text(
            json.dumps(preferences, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _assert_onboarding_payload(self, payload: dict) -> None:
        """onboarding 必须明确可多选并一次列出四个市场，不得暗示单选。"""
        self.assertEqual(payload["status"], "onboarding_required")
        self.assertIs(payload["multi_select"], True)
        self.assertEqual(
            [(option["market_scope"], option["label"]) for option in payload["options"]],
            [("us", "美股"), ("hk", "港股"), ("a_share", "A 股"), ("ah_compare", "A/H 对比")],
        )
        self.assertIn("可多选", payload["detail"])
        self.assertNotIn("单选", payload["detail"].replace("不是单选", ""))

    def test_show_requires_onboarding_when_unconfigured(self) -> None:
        payload = self._run_ok("show")
        self._assert_onboarding_payload(payload)
        self.assertNotIn("ticker_hint", payload)

    def test_resolve_requires_onboarding_when_unconfigured(self) -> None:
        for args in (["resolve", "--query", "AAPL"], ["resolve", "--query", "600519"]):
            payload = self._run_ok(*args)
            self.assertEqual(payload["status"], "onboarding_required")
            self.assertNotIn("market_scope", payload)

    def test_onboarding_for_bare_ticker_is_multi_select_with_quick_hint(self) -> None:
        # 回归样例：未配置偏好时输入裸 ticker LITE，响应必须说明市场可多选、
        # 展示四个可一次多选的市场，并保留“美股时可确认 LITE（NASDAQ）”快捷提示。
        for query in ("lite", "LITE"):
            with self.subTest(query=query):
                payload = self._run_ok("resolve", "--query", query)
                self._assert_onboarding_payload(payload)
                self.assertEqual(payload["ticker_hint"], "美股时可确认 LITE（NASDAQ）。")

    def test_empty_or_missing_enabled_scopes_require_onboarding(self) -> None:
        for preferences in (
            {"schema_version": 1, "enabled_market_scopes": []},
            {"schema_version": 1},
        ):
            self._write_preferences(preferences)
            self._assert_onboarding_payload(self._run_ok("show"))
            for args in (
                ["resolve", "--query", "AAPL"],
                ["resolve", "--query", "600519"],
            ):
                payload = self._run_ok(*args)
                self.assertEqual(payload["status"], "onboarding_required")
                self.assertIs(payload["multi_select"], True)

    def test_non_schema_version_1_is_rejected(self) -> None:
        for preferences in (
            {"schema_version": 0, "enabled_market_scopes": ["us"]},
            {"schema_version": 2, "enabled_market_scopes": ["us"]},
            {"schema_version": "1", "enabled_market_scopes": ["us"]},
            {"enabled_market_scopes": ["us"]},
        ):
            self._write_preferences(preferences)
            for args in (["show"], ["resolve", "--query", "AAPL"]):
                result = self._run(*args)
                self.assertNotEqual(result.returncode, 0, msg=args)
                self.assertIn("schema_version 1", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_set_multi_select_saves_reads_and_ah_compare_auto_expands(self) -> None:
        saved = self._set_scopes("us,ah_compare", default="us")
        scopes = saved["preferences"]["enabled_market_scopes"]
        self.assertEqual(scopes, ["us", "hk", "a_share", "ah_compare"])
        self.assertEqual(saved["ah_compare_auto_expanded"], ["hk", "a_share"])

        shown = self._run_ok("show")
        self.assertEqual(shown["status"], "ok")
        self.assertEqual(shown["preferences"]["enabled_market_scopes"], scopes)
        self.assertEqual(shown["preferences"]["default_market_scope"], "us")
        self.assertEqual(shown["preferences"]["schema_version"], 1)

        modified = self._set_scopes("us")
        self.assertEqual(
            modified["preferences"]["enabled_market_scopes"], ["us"]
        )
        self.assertEqual(self._read_preferences_file()["enabled_market_scopes"], ["us"])

    def test_set_rejects_unknown_scope_and_bad_default(self) -> None:
        result = self._run("set", "--scopes", "us,moon")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("moon", result.stderr)

        result = self._run("set", "--scopes", "us", "--default", "hk")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--default", result.stderr)
        self.assertFalse((self.workspace / PREFERENCES_FILENAME).exists())

    def test_explicit_suffix_takes_priority(self) -> None:
        self._set_scopes("us,hk,a_share")
        cases = {
            "0700.HK": ("hk", "explicit_suffix"),
            "600519.SS": ("a_share", "explicit_suffix"),
            "000001.SZ": ("a_share", "explicit_suffix"),
            "600519.SH": ("a_share", "explicit_suffix"),
            "BABA.US": ("us", "explicit_suffix"),
        }
        for query, (scope, reason) in cases.items():
            payload = self._run_ok("resolve", "--query", query)
            self.assertEqual(payload["status"], "resolved", msg=query)
            self.assertEqual(payload["market_scope"], scope, msg=query)
            self.assertEqual(payload["reason"], reason, msg=query)
            self.assertEqual(payload["enabled_via"], "preferences")

    def test_bare_alpha_resolves_us_when_us_is_sole_base_scope(self) -> None:
        self._set_scopes("us")
        payload = self._run_ok("resolve", "--query", "AAPL")
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["market_scope"], "us")
        self.assertEqual(payload["reason"], "bare_alpha_ticker")
        self.assertEqual(payload["enabled_via"], "preferences")

    def test_lite_token_is_ticker_candidate_not_a_mode(self) -> None:
        # 回归样例：LITE 是 Lumentum 的美股 ticker。单个 1–5 位字母 token
        # （大小写不敏感）必须识别为 ticker 候选，绝不视为“lite 模式”。
        self._set_scopes("us")
        for query in ("lite", "Lite", "LITE"):
            with self.subTest(query=query):
                payload = self._run_ok("resolve", "--query", query)
                self.assertEqual(payload["status"], "resolved")
                self.assertEqual(payload["market_scope"], "us")
                self.assertEqual(payload["reason"], "bare_alpha_ticker")
                candidate = payload["listing_candidates"][0]
                self.assertEqual(candidate["symbol"], "LITE")
                self.assertEqual(candidate["exchange"], "NYSE/NASDAQ")

        # 美股不是唯一已启用基础范围时：识别为 ticker 候选，
        # 只要求选择市场/交易所，不要求先提供公司名+ticker+交易所。
        self._set_scopes("us,hk")
        payload = self._run_ok("resolve", "--query", "lite")
        self.assertEqual(payload["status"], "ambiguous")
        self.assertTrue(payload["needs_user_selection"])
        self.assertEqual(payload["reason"], "bare_alpha_multiple_scopes")
        self.assertEqual(payload["query_kind"], "ticker")
        self.assertEqual(payload["query"], "LITE")
        self.assertIn("市场/交易所", payload["detail"])
        self.assertNotIn("公司名", payload["detail"].replace("不要求先提供公司名", ""))
        self.assertNotIn("listing_candidates", payload)

    def test_bare_alpha_ambiguous_with_multiple_enabled_scopes(self) -> None:
        self._set_scopes("us,hk")
        payload = self._run_ok("resolve", "--query", "AAPL")
        self.assertEqual(payload["status"], "ambiguous")
        self.assertTrue(payload["needs_user_selection"])
        self.assertEqual(payload["reason"], "bare_alpha_multiple_scopes")
        self.assertEqual(
            {c["market_scope"] for c in payload["candidates"]}, {"us", "hk"}
        )
        self.assertNotIn("listing_candidates", payload)

        self._set_scopes("us,ah_compare")
        payload = self._run_ok("resolve", "--query", "AAPL")
        self.assertEqual(payload["status"], "ambiguous")
        self.assertEqual(payload["reason"], "bare_alpha_multiple_scopes")
        self.assertEqual(
            {c["market_scope"] for c in payload["candidates"]},
            {"us", "hk", "a_share", "ah_compare"},
        )

    def test_bare_alpha_once_scope_boundary(self) -> None:
        self._set_scopes("us")
        payload = self._run_ok("resolve", "--query", "AAPL", "--once-scope", "hk")
        self.assertEqual(payload["status"], "ambiguous")
        self.assertEqual(payload["reason"], "bare_alpha_multiple_scopes")
        self.assertEqual(
            {c["market_scope"] for c in payload["candidates"]}, {"us", "hk"}
        )

        # ah_compare 蕴含 hk 与 a_share 基础范围；再叠加 --once-scope us 后，
        # 裸字母 ticker 命中多个已启用基础范围，必须 ambiguous 而非静默归为 us。
        self._write_preferences(
            {
                "schema_version": 1,
                "enabled_market_scopes": ["ah_compare"],
                "default_market_scope": None,
            }
        )
        payload = self._run_ok("resolve", "--query", "AAPL", "--once-scope", "us")
        self.assertEqual(payload["status"], "ambiguous")
        self.assertTrue(payload["needs_user_selection"])
        self.assertEqual(payload["reason"], "bare_alpha_multiple_scopes")
        self.assertEqual(
            {c["market_scope"] for c in payload["candidates"]},
            {"us", "hk", "a_share", "ah_compare"},
        )
        self.assertNotIn("listing_candidates", payload)

    def test_bare_alpha_ambiguous_when_ah_compare_enables_multiple_base_scopes(
        self,
    ) -> None:
        self._write_preferences(
            {"schema_version": 1, "enabled_market_scopes": ["ah_compare", "us"]}
        )
        payload = self._run_ok("resolve", "--query", "AAPL")
        self.assertEqual(payload["status"], "ambiguous")
        self.assertTrue(payload["needs_user_selection"])
        self.assertEqual(payload["reason"], "bare_alpha_multiple_scopes")
        self.assertEqual(
            {c["market_scope"] for c in payload["candidates"]},
            {"us", "hk", "a_share", "ah_compare"},
        )
        self.assertNotIn("listing_candidates", payload)

    def test_bare_code_out_of_scope_then_resolved_when_enabled(self) -> None:
        self._set_scopes("us")
        payload = self._run_ok("resolve", "--query", "600519")
        self.assertEqual(payload["status"], "out_of_scope")
        self.assertEqual(payload["market_scope"], "a_share")

        self._set_scopes("us,a_share")
        payload = self._run_ok("resolve", "--query", "600519")
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["market_scope"], "a_share")
        candidate = payload["listing_candidates"][0]
        self.assertEqual(candidate["symbol"], "600519.SS")
        self.assertEqual(candidate["exchange"], "SSE")
        self.assertEqual(candidate["currency"], "CNY")

    def test_bare_code_hk_and_shenzhen_classification(self) -> None:
        self._set_scopes("hk,a_share")
        hk_payload = self._run_ok("resolve", "--query", "700")
        self.assertEqual(hk_payload["status"], "resolved")
        self.assertEqual(hk_payload["market_scope"], "hk")
        self.assertEqual(hk_payload["listing_candidates"][0]["symbol"], "0700.HK")

        sz_payload = self._run_ok("resolve", "--query", "300750")
        self.assertEqual(sz_payload["status"], "resolved")
        self.assertEqual(sz_payload["listing_candidates"][0]["symbol"], "300750.SZ")

    def test_out_of_scope_offers_exactly_two_options(self) -> None:
        self._set_scopes("hk")
        payload = self._run_ok("resolve", "--query", "AAPL.US")
        self.assertEqual(payload["status"], "out_of_scope")
        self.assertEqual(payload["market_scope"], "us")
        self.assertEqual(payload["options"], ["once", "add_to_scope"])
        self.assertEqual(self._read_preferences_file()["enabled_market_scopes"], ["hk"])

    def test_bare_alpha_ambiguous_when_us_is_not_sole_base_scope(self) -> None:
        # 裸字母 ticker 只有在美股是唯一已启用基础范围时才 resolved；
        # 仅 HK、仅 A 股、HK+A 都必须 ambiguous/needs_user_selection，
        # 候选只列已启用市场，绝不返回 out_of_scope/us。
        for scopes, expected_candidates in (
            ("hk", {"hk"}),
            ("a_share", {"a_share"}),
            ("hk,a_share", {"hk", "a_share"}),
        ):
            with self.subTest(scopes=scopes):
                self._set_scopes(scopes)
                payload = self._run_ok("resolve", "--query", "LITE")
                self.assertEqual(payload["status"], "ambiguous")
                self.assertTrue(payload["needs_user_selection"])
                self.assertEqual(payload["reason"], "bare_alpha_multiple_scopes")
                self.assertEqual(payload["query_kind"], "ticker")
                self.assertEqual(payload["query"], "LITE")
                self.assertEqual(
                    {c["market_scope"] for c in payload["candidates"]},
                    expected_candidates,
                )
                self.assertNotIn("listing_candidates", payload)
                self.assertNotIn("market_scope", payload)
                self.assertNotIn("options", payload)

        self._set_scopes("us")
        payload = self._run_ok("resolve", "--query", "LITE")
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["market_scope"], "us")
        self.assertEqual(payload["reason"], "bare_alpha_ticker")

    def test_once_scope_resolves_without_persisting(self) -> None:
        self._set_scopes("us")
        before = self._read_preferences_file()
        payload = self._run_ok("resolve", "--query", "0700.HK", "--once-scope", "hk")
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["market_scope"], "hk")
        self.assertEqual(payload["enabled_via"], "once_scope")
        self.assertFalse(payload["persisted"])
        self.assertEqual(self._read_preferences_file(), before)

    def test_name_query_is_ambiguous_and_needs_user_selection(self) -> None:
        self._set_scopes("us,hk")
        payload = self._run_ok("resolve", "--query", "贵州茅台")
        self.assertEqual(payload["status"], "ambiguous")
        self.assertTrue(payload["needs_user_selection"])
        self.assertEqual(payload["reason"], "name_unresolvable_locally")
        self.assertEqual(
            {c["market_scope"] for c in payload["candidates"]}, {"us", "hk"}
        )

    def test_ah_pair_format_only_needs_user_selection(self) -> None:
        payload = self._run_ok("resolve", "--ah-pair", "600519.SS,0700.HK")
        self.assertEqual(payload["pair_status"], "needs_user_selection")
        self.assertIn("reason", payload)
        self.assertIn("发行人", payload["reason"])
        self.assertEqual(payload["a_share"]["market_scope"], "a_share")
        self.assertEqual(payload["a_share"]["currency"], "CNY")
        self.assertEqual(payload["hk"]["market_scope"], "hk")
        self.assertEqual(payload["hk"]["currency"], "HKD")
        self.assertEqual(payload["fx_pair"], "CNY/HKD")
        self.assertEqual(
            payload["must_report"],
            [
                "fx_rate",
                "share_right_ratio",
                "liquidity_diff",
                "trading_day_diff",
                "premium_discount",
            ],
        )
        self.assertNotIn("identity_note", payload)

    def test_ah_pair_ok_with_asserted_shared_issuer(self) -> None:
        ok = self._run_ok(
            "resolve", "--ah-pair", "600519.SS,0700.HK", "--ah-issuer-id", "贵州茅台"
        )
        self.assertEqual(ok["pair_status"], "ok")
        self.assertEqual(ok["asserted_issuer_id"], "贵州茅台")
        self.assertIn("未做本地核验", ok["identity_note"])
        self.assertEqual(ok["a_share"]["market_scope"], "a_share")
        self.assertEqual(ok["hk"]["market_scope"], "hk")
        self.assertEqual(ok["fx_pair"], "CNY/HKD")
        self.assertEqual(
            ok["must_report"],
            [
                "fx_rate",
                "share_right_ratio",
                "liquidity_diff",
                "trading_day_diff",
                "premium_discount",
            ],
        )

        also_ok = self._run_ok(
            "resolve",
            "--ah-pair",
            "600519.SS,0700.HK",
            "--ah-issuer-id",
            "贵州茅台,贵州茅台",
        )
        self.assertEqual(also_ok["pair_status"], "ok")
        self.assertEqual(also_ok["asserted_issuer_id"], "贵州茅台")

    def test_ah_pair_mismatched_issuer_ids_fail(self) -> None:
        failed = self._run_ok(
            "resolve",
            "--ah-pair",
            "600519.SS,0700.HK",
            "--ah-issuer-id",
            "贵州茅台,腾讯控股",
        )
        self.assertEqual(failed["pair_status"], "failed")
        self.assertIn("不一致", failed["reason"])
        self.assertIn("贵州茅台", failed["reason"])
        self.assertIn("腾讯控股", failed["reason"])

    def test_ah_pair_failed(self) -> None:
        failed = self._run_ok("resolve", "--ah-pair", "0700.HK,9988.HK")
        self.assertEqual(failed["pair_status"], "failed")
        self.assertIn("reason", failed)

        result = self._run("resolve", "--query", "AAPL", "--ah-issuer-id", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--ah-pair", result.stderr)


class ProviderResolutionTests(unittest.TestCase):
    def test_default_resolution_is_explicit_and_recorded(self) -> None:
        record = providers.resolve_provider("hk")
        self.assertEqual(record["provider"], "yfinance")
        self.assertEqual(record["classification"], "non_official_best_effort")
        self.assertEqual(record["market_scope"], "hk")
        self.assertTrue(record["resolved_as_of"])
        self.assertIn("provider=yfinance", record["ledger_note"])
        self.assertIn("market_scope=hk", record["ledger_note"])
        self.assertNotIn("switch_note", record)

    def test_requested_provider_wins_and_switch_is_recorded(self) -> None:
        record = providers.resolve_provider("us", requested="offline_fixture")
        self.assertEqual(record["provider"], "offline_fixture")
        self.assertIn("switch_note", record)
        self.assertIn("offline_fixture", record["switch_note"])
        self.assertIn("no silent fallback", record["switch_note"])

    def test_unsupported_market_scope_fails_closed(self) -> None:
        with self.assertRaises(providers.UnsupportedProviderError):
            providers.resolve_provider("ah_compare")
        with self.assertRaises(providers.UnsupportedProviderError):
            providers.resolve_provider("moon")

    def test_unknown_requested_provider_fails_closed_without_fallback(self) -> None:
        with self.assertRaises(providers.UnsupportedProviderError):
            providers.resolve_provider("us", requested="bloomberg")

    def test_registry_adapters_cover_three_base_markets(self) -> None:
        for name, adapter in providers.ADAPTERS.items():
            self.assertEqual(
                set(adapter["market_scopes"]), {"us", "hk", "a_share"}, msg=name
            )
            self.assertTrue(adapter["classification"], msg=name)

    def test_cli_outputs_resolution_record(self) -> None:
        result = subprocess.run(
            [sys.executable, str(PROVIDERS), "--market-scope", "hk"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["provider"], "yfinance")
        self.assertEqual(record["market_scope"], "hk")

        result = subprocess.run(
            [sys.executable, str(PROVIDERS), "--market-scope", "ah_compare"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ah_compare", result.stderr)


if __name__ == "__main__":
    unittest.main()
