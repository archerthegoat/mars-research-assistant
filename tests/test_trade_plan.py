from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = (
    ROOT
    / "skills"
    / "mars-research-assistant"
    / "skills"
    / "deep-equity-research"
)
TRADE_PLAN = PACKAGE / "scripts" / "trade_plan.py"
DCF = PACKAGE / "scripts" / "dcf.py"
EARNINGS_QUALITY = PACKAGE / "scripts" / "earnings_quality.py"
RULES_PATH = PACKAGE / "reference" / "preregistered_rules.json"
FIXTURES = ROOT / "tests" / "fixtures"

TRADE_DIRECTIVE = re.compile(
    r"买入|卖出|增持|减持|加仓|减仓|建仓|平仓|下单|持仓比例|\bbuy\b|\bsell\b|"
    r"\bposition size\b|\bplace (?:an )?order\b",
    re.IGNORECASE,
)

IDENTITY = {
    "issuer_id": "issuer-tradeplan",
    "listing_id": "TP.US",
    "case_id": "case-tradeplan-001",
    "artifact_version": 1,
    "schema_version": 1,
}

# tests/fixtures/technical-evidence-*.json are hand-built minimal evidence
# payloads (schema_version / identity / status / evidence_id / as_of / ohlcv /
# indicators.latest / key_levels) shaped after technical_analysis.py's
# evidence dict; they are inputs to trade_plan.py only, not outputs of the
# technical-analysis skill.

AH_A_SHARE_IDENTITY = {
    "issuer_id": "issuer-tradeplan",
    "listing_id": "TP.SS",
    "case_id": "case-tradeplan-001",
    "artifact_version": 1,
    "schema_version": 1,
}
AH_HK_IDENTITY = {**AH_A_SHARE_IDENTITY, "listing_id": "TP.HK"}


def _run(script: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        capture_output=True,
        text=True,
        env=environment,
    )


class TradePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _engine_output(
        self, script: Path, fixture_name: str, stem: str, identity: dict | None = None
    ) -> dict:
        """Run a v1.0.3 engine end-to-end so trade-plan inputs stay recomputable."""
        payload = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
        payload["identity"] = dict(identity or IDENTITY)
        if identity is not None and payload["identity"]["listing_id"].endswith(".HK"):
            payload["market_scope"] = "hk"
            payload["currency"] = "HKD"
        input_path = self.tmp / f"{stem}-input.json"
        input_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output_path = self.tmp / f"{stem}.json"
        result = _run(script, ["--input", str(input_path), "--output", str(output_path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output_path.read_text(encoding="utf-8"))

    def _valuation(self) -> dict:
        return self._engine_output(DCF, "valuation-full.json", "valuation")

    def _earnings(self, fixture_name: str = "earnings-quality-a.json") -> dict:
        return self._engine_output(EARNINGS_QUALITY, fixture_name, "earnings-quality")

    def _evidence_path(self, evidence: dict, stem: str = "evidence") -> Path:
        path = self.tmp / f"{stem}.json"
        path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def _fixture_evidence(self, name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def _run_trade_plan(
        self,
        valuation: dict,
        evidence_path: Path,
        earnings: dict,
        thesis_override: dict | None = None,
        html: bool = False,
        output_name: str = "trade-plan.json",
    ) -> tuple[subprocess.CompletedProcess[str], dict | None, Path]:
        valuation_path = self._evidence_path(valuation, "valuation-in")
        earnings_path = self._evidence_path(earnings, "earnings-in")
        thesis = json.loads((FIXTURES / "thesis-pass.json").read_text(encoding="utf-8"))
        if thesis_override:
            thesis.update(thesis_override)
        thesis_path = self._evidence_path(thesis, "thesis-in")
        output_path = self.tmp / output_name
        arguments = [
            "--valuation", str(valuation_path),
            "--evidence", str(evidence_path),
            "--earnings-quality", str(earnings_path),
            "--thesis", str(thesis_path),
            "--output", str(output_path),
        ]
        if html:
            arguments += ["--html", str(self.tmp / "trade-plan.html")]
        result = _run(TRADE_PLAN, arguments)
        plan = None
        if output_path.exists():
            plan = json.loads(output_path.read_text(encoding="utf-8"))
        return result, plan, output_path

    def _expected_zones(self, valuation: dict, evidence: dict) -> dict:
        rules = self.rules
        zone = valuation["results"]["dcf"]["value_zone"]
        margin = 1 - rules["safety_margin"]
        value_band = {
            "low": round(zone["low"] * margin, 6),
            "high": round(zone["high"] * margin, 6),
        }
        latest = evidence["indicators"]["latest"]
        close = latest["close"]
        atr = latest["atr14"]
        supports = [
            level for level in evidence["key_levels"] if level["side"] == "support"
        ]
        resistances = [
            level for level in evidence["key_levels"] if level["side"] == "resistance"
        ]
        support = min(supports, key=lambda level: abs(level["price"] - close))
        resistance = min(resistances, key=lambda level: abs(level["price"] - close))
        tolerance = rules["entry_atr_tolerance"] * atr
        entry_low = round(max(value_band["low"], round(support["price"] - tolerance, 6)), 6)
        entry_high = round(min(value_band["high"], round(support["price"] + tolerance, 6)), 6)
        invalidation = round(
            support["price"] - rules["invalidation_atr_multiple"] * atr, 6
        )
        risk = entry_low - invalidation
        weighted = valuation["results"]["dcf"]["probability_weighted_per_share"]
        return {
            "value_band": value_band,
            "entry": {"low": entry_low, "high": entry_high},
            "invalidation": invalidation,
            "technical_target": round(resistance["price"], 6),
            "fundamental_target": round(weighted, 6),
            "downside_pct": round(risk / entry_low, 6),
            "reward_risk": round((weighted - entry_high) / risk, 6),
        }

    def test_full_pass_entry_plan_with_recomputed_zones(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["status"], "entry_plan")
        self.assertIsNone(plan["veto"])
        self.assertTrue(all(gate["pass"] for gate in plan["gates"].values()))
        self.assertEqual(plan["rules_version"], "v1.0.3-rules-1")
        self.assertEqual(plan["direction"], "long_only")
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["identity"]["case_id"], IDENTITY["case_id"])
        self.assertEqual(plan["identity"]["schema_version"], 1)

        expected = self._expected_zones(valuation, evidence)
        self.assertEqual(plan["entry_plan"]["zone"], expected["entry"])
        self.assertEqual(plan["entry_plan"]["value_band"], expected["value_band"])
        self.assertEqual(
            plan["invalidation_plan"]["technical_invalidation"]["level"],
            expected["invalidation"],
        )
        self.assertEqual(
            plan["target_plan"]["technical_target"]["level"],
            expected["technical_target"],
        )
        self.assertEqual(
            plan["target_plan"]["fundamental_target"]["level"],
            expected["fundamental_target"],
        )
        self.assertEqual(plan["downside_pct"], expected["downside_pct"])
        self.assertEqual(plan["reward_risk_ratio"], expected["reward_risk"])
        self.assertEqual(
            plan["references"]["evidence_id"], evidence["evidence_id"]
        )
        self.assertIn(IDENTITY["case_id"], plan["references"]["valuation_id"])
        self.assertIn(IDENTITY["case_id"], plan["references"]["earnings_quality_id"])
        # 技术目标与基本面目标分别标注，不混作一个数字。
        self.assertNotEqual(
            plan["target_plan"]["technical_target"]["level"],
            plan["target_plan"]["fundamental_target"]["level"],
        )

    def test_html_generated_and_numbers_match_json(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings, html=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        html_path = self.tmp / "trade-plan.html"
        self.assertTrue(html_path.exists())
        html = html_path.read_text(encoding="utf-8")
        # 单文件离线 HTML：不引用外部资源。
        self.assertNotIn("src=", html)
        self.assertNotIn("href=", html)
        # 图上的关键数值必须来自 trade-plan.json，不创造新价位。
        for value in (
            plan["entry_plan"]["zone"]["low"],
            plan["entry_plan"]["zone"]["high"],
            plan["entry_plan"]["value_band"]["low"],
            plan["entry_plan"]["value_band"]["high"],
            plan["invalidation_plan"]["technical_invalidation"]["level"],
            plan["target_plan"]["technical_target"]["level"],
            plan["target_plan"]["fundamental_target"]["level"],
        ):
            self.assertIn(json.dumps(value), html)
        self.assertFalse(TRADE_DIRECTIVE.search(html))

    def test_driver_dcf_usable_becomes_value_source(self) -> None:
        # 通用质量门槛为 usable 的驱动型 DCF 优先于 baseline DCF 成为
        # 基本面目标来源（basis=driver_dcf），zone/target 取自驱动型结果。
        valuation = self._valuation()
        baseline = valuation["results"]["dcf"]
        zone = baseline["value_zone"]
        weighted = baseline["probability_weighted_per_share"]
        valuation["results"]["driver_dcf"] = {
            "model_kind": "driver_dcf",
            "model_version": "v1.0.3-valuation-1",
            "status": "computed",
            "scenarios": [],
            "probability_weighted_per_share": weighted,
            "value_zone": {"low": zone["low"], "high": zone["high"]},
            "terminal_value_checks": {
                name: {"status": "pass", "detail": "离线验收示例：终值检查。"}
                for name in (
                    "long_run_growth",
                    "mature_margin",
                    "reinvestment_roic_consistency",
                )
            },
            "quality": {
                "status": "usable",
                "flags": [],
                "reasons": ["全部质量检查通过，可形成基本面参考值。"],
            },
        }
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "entry_plan")
        target = plan["target_plan"]["fundamental_target"]
        self.assertEqual(target["basis"], "driver_dcf")
        self.assertEqual(target["level"], round(weighted, 6))
        self.assertEqual(plan["gates"]["valuation"]["pass"], True)
        self.assertIn("驱动型 DCF", plan["gates"]["valuation"]["reason"])

    def test_pe_usable_becomes_value_source_when_dcf_is_not_applicable(self) -> None:
        valuation = self._valuation()
        valuation["results"]["dcf"] = {
            "status": "not_applicable",
            "reason": "离线验收示例：改用 PE。",
        }
        valuation["results"]["pe"] = {
            "model_kind": "pe_multiple",
            "model_version": "v1.0.3-valuation-1",
            "method": "pe",
            "status": "computed",
            "earnings_basis": "forward_eps",
            "basis_rationale": "离线验收示例：前瞻 EPS。",
            "scenarios": [
                {"name": "bear", "probability": 0.25, "eps": 1.0, "pe_multiple": 12.0, "per_share": 12.0},
                {"name": "base", "probability": 0.5, "eps": 1.0, "pe_multiple": 14.0, "per_share": 14.0},
                {"name": "bull", "probability": 0.25, "eps": 1.0, "pe_multiple": 16.0, "per_share": 16.0},
            ],
            "probability_weighted_eps": 1.0,
            "probability_weighted_per_share": 14.0,
            "value_zone": {"low": 12.0, "high": 14.0},
            "quality": {
                "status": "usable",
                "flags": [],
                "reasons": ["离线验收示例：PE 质量门槛通过。"],
            },
        }
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings,
            output_name="trade-plan-pe.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["target_plan"]["fundamental_target"]["basis"], "pe")
        self.assertEqual(plan["target_plan"]["fundamental_target"]["level"], 14.0)
        self.assertTrue(plan["gates"]["valuation"]["pass"])
        self.assertIn("PE 模型", plan["gates"]["valuation"]["reason"])

    def test_driver_dcf_below_gate_forms_no_fundamental_target(self) -> None:
        # 已提供 driver_model 但质量门槛非 usable：不形成基本面目标，也
        # 不得回退把旧 baseline DCF 包装成目标价。
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        for quality_status in ("conditional", "unreliable"):
            with self.subTest(quality_status=quality_status):
                valuation["results"]["driver_dcf"] = {
                    "model_kind": "driver_dcf",
                    "model_version": "v1.0.3-valuation-1",
                    "status": "computed",
                    "scenarios": [],
                    "probability_weighted_per_share": 180.0,
                    "value_zone": {"low": 150.0, "high": 180.0},
                    "terminal_value_checks": {
                        name: {"status": "pass", "detail": "离线验收示例：终值检查。"}
                        for name in (
                            "long_run_growth",
                            "mature_margin",
                            "reinvestment_roic_consistency",
                        )
                    },
                    "quality": {
                        "status": quality_status,
                        "flags": ["short_forecast_horizon"],
                        "reasons": ["显式预测期短于 5 年。"],
                    },
                }
                result, plan, _ = self._run_trade_plan(
                    valuation,
                    self._evidence_path(evidence),
                    earnings,
                    output_name=f"trade-plan-{quality_status}.json",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(plan["status"], "watch")
                self.assertFalse(plan["gates"]["valuation"]["pass"])
                self.assertIsNone(plan["target_plan"]["fundamental_target"]["level"])
                self.assertIsNone(plan["entry_plan"]["zone"])

    def test_earnings_quality_c_grade_veto(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings("earnings-quality-c.json")
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse(plan["gates"]["earnings_quality"]["pass"])
        self.assertIn("财报质量", plan["veto"]["reason"])
        self.assertIsNone(plan["entry_plan"]["zone"])
        self.assertIsNone(plan["target_plan"]["fundamental_target"]["level"])
        self.assertIsNone(
            plan["invalidation_plan"]["technical_invalidation"]["level"]
        )
        self.assertTrue(plan["entry_plan"]["what_would_change"])

    def test_stale_evidence_watch_without_zones(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-stale.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse(plan["gates"]["technical_evidence"]["pass"])
        self.assertIn("过期", plan["gates"]["technical_evidence"]["reason"])
        self.assertIsNone(plan["entry_plan"]["zone"])
        self.assertIsNone(plan["downside_pct"])
        self.assertIsNone(plan["reward_risk_ratio"])

    def test_unqualified_evidence_watch(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence["status"] = "rejected"
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse(plan["gates"]["technical_evidence"]["pass"])
        self.assertIsNone(plan["entry_plan"]["zone"])

    def test_malformed_evidence_id_fails_closed(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        for bad_id in (
            "1111111111111111111111111111111111111111111111111111111111111111",
            "sha256:1111",
            "sha256:111111111111111111111111111111111111111111111111111111111111111G",
            "sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "md5:1111111111111111111111111111111111111111111111111111111111111111",
        ):
            with self.subTest(evidence_id=bad_id):
                evidence = self._fixture_evidence("technical-evidence-qualified.json")
                evidence["evidence_id"] = bad_id
                result, plan, _ = self._run_trade_plan(
                    valuation, self._evidence_path(evidence), earnings
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("evidence_id", result.stderr)
                self.assertIn("sha256:<64 lowercase hex>", result.stderr)
                self.assertIsNone(plan)

    def test_missing_resistance_downgrades_to_watch(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-no-resistance.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings, html=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse(plan["gates"]["technical_evidence"]["pass"])
        self.assertIn("阻力位", plan["gates"]["technical_evidence"]["reason"])
        self.assertEqual(plan["veto"]["gate"], "technical_evidence")
        # 缺阻力位时不产生任何价格区间，也不生成 HTML 注释图。
        self.assertIsNone(plan["entry_plan"]["zone"])
        self.assertIsNone(plan["target_plan"]["technical_target"]["level"])
        self.assertIsNone(plan["downside_pct"])
        self.assertIsNone(plan["reward_risk_ratio"])
        self.assertTrue(plan["entry_plan"]["what_would_change"])
        self.assertTrue(any("阻力位" in gap for gap in plan["data_gaps"]))
        self.assertFalse((self.tmp / "trade-plan.html").exists())
        self.assertIn("watch", result.stderr)

    def test_no_intersection_watch_with_wait_conditions(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-no-intersection.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse(plan["gates"]["value_technical_intersection"]["pass"])
        self.assertEqual(plan["veto"]["gate"], "value_technical_intersection")
        self.assertIsNone(plan["entry_plan"]["zone"])
        self.assertTrue(plan["entry_plan"]["what_would_change"])
        self.assertTrue(plan["entry_plan"]["trigger_conditions"])

    def test_reward_risk_below_minimum_watch(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        # ATR 放大后失效位远离入场下沿，收益风险比低于预注册下限 2.0。
        evidence["indicators"]["latest"]["close"] = 13.5
        evidence["indicators"]["latest"]["atr14"] = 2.0
        evidence["key_levels"] = [
            {"side": "support", "price": 13.0, "kind": "swing_cluster"},
            {"side": "resistance", "price": 15.0, "kind": "swing_cluster"},
        ]
        expected = self._expected_zones(valuation, evidence)
        self.assertLess(expected["reward_risk"], self.rules["min_reward_risk_ratio"])
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse(plan["gates"]["reward_risk"]["pass"])
        self.assertEqual(plan["veto"]["gate"], "reward_risk")
        self.assertIsNone(plan["entry_plan"]["zone"])

    def test_case_id_mismatch_fails_closed(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, output_path = self._run_trade_plan(
            valuation,
            self._evidence_path(evidence),
            earnings,
            thesis_override={
                "identity": {**IDENTITY, "case_id": "case-other-999"}
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("case_id", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_issuer_id_mismatch_fails_closed(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, output_path = self._run_trade_plan(
            valuation,
            self._evidence_path(evidence),
            earnings,
            thesis_override={
                "identity": {**IDENTITY, "issuer_id": "issuer-other-999"}
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("issuer_id", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def _ah_valuation_with(self, mutate) -> dict:
        payload = json.loads(
            (FIXTURES / "valuation-ah-compare.json").read_text(encoding="utf-8")
        )
        payload["identity"] = dict(AH_A_SHARE_IDENTITY)
        mutate(payload)
        input_path = self.tmp / "valuation-ah-input.json"
        input_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output_path = self.tmp / "valuation-ah.json"
        result = _run(DCF, ["--input", str(input_path), "--output", str(output_path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output_path.read_text(encoding="utf-8"))

    def _ah_trade_plan(
        self, valuation: dict | None = None
    ) -> tuple[subprocess.CompletedProcess[str], dict | None, Path]:
        """A/H 输入组：估值挂在 A 股 TP.SS，其余输入挂在港股 TP.HK。"""
        if valuation is None:
            valuation = self._ah_valuation_with(lambda payload: None)
        earnings = self._engine_output(
            EARNINGS_QUALITY,
            "earnings-quality-a.json",
            "earnings-quality",
            identity=AH_HK_IDENTITY,
        )
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence["identity"] = dict(AH_HK_IDENTITY)
        # 证据绑定在港股 listing 上，symbol 必须与 identity listing_id 一致。
        evidence["symbol"] = "TP.HK"
        return self._run_trade_plan(
            valuation,
            self._evidence_path(evidence),
            earnings,
            thesis_override={"identity": dict(AH_HK_IDENTITY)},
        )

    def test_ah_compare_pair_with_disclosures_accepted(self) -> None:
        # A/H 对比：同一 issuer_id 与 case_id 挂一 A 一 H 两个 listing_id，
        # 且估值工件携带 CNY/HKD 汇率与五项强制披露时合法。
        result, plan, _ = self._ah_trade_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "entry_plan")
        self.assertEqual(plan["market_scope"], "ah_compare")
        # 方案的 identity 沿用 valuation 的 listing_id。
        self.assertEqual(plan["identity"]["listing_id"], "TP.SS")
        ah_compare = plan["ah_compare"]
        self.assertEqual(
            ah_compare["pair"],
            {"a_share_listing_id": "TP.SS", "hk_listing_id": "TP.HK"},
        )
        self.assertEqual(ah_compare["fx_pair"], "CNY/HKD")
        self.assertEqual(ah_compare["fx_rate"], 1.09)
        self.assertEqual(ah_compare["share_right_ratio"], 1.0)
        self.assertEqual(ah_compare["premium_discount"], 0.12)
        self.assertTrue(ah_compare["liquidity_diff"])
        self.assertTrue(ah_compare["trading_day_diff"])

    def test_ah_compare_missing_block_fails_closed(self) -> None:
        valuation = self._ah_valuation_with(lambda payload: payload.pop("ah_compare"))
        result, plan, output_path = self._ah_trade_plan(valuation)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ah_compare", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_ah_compare_missing_disclosure_fails_closed(self) -> None:
        def mutate(payload: dict) -> None:
            del payload["ah_compare"]["premium_discount"]

        valuation = self._ah_valuation_with(mutate)
        result, plan, output_path = self._ah_trade_plan(valuation)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("premium_discount", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_ah_compare_pair_mismatch_fails_closed(self) -> None:
        def mutate(payload: dict) -> None:
            payload["ah_compare"]["pair"] = {
                "a_share_listing_id": "OTHER.SS",
                "hk_listing_id": "OTHER.HK",
            }

        valuation = self._ah_valuation_with(mutate)
        result, plan, output_path = self._ah_trade_plan(valuation)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法唯一配对", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_two_listings_without_ah_scope_fail_closed(self) -> None:
        # 默认情况下所有输入的 listing_id 必须一致；非 ah_compare 出现两个
        # listing 立即失败。
        valuation = self._valuation()
        earnings = self._engine_output(
            EARNINGS_QUALITY,
            "earnings-quality-a.json",
            "earnings-quality",
            identity=AH_HK_IDENTITY,
        )
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, output_path = self._run_trade_plan(
            valuation,
            self._evidence_path(evidence),
            earnings,
            thesis_override={"identity": dict(AH_HK_IDENTITY)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("listing_id mismatch", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_vie_adr_records_identifications_with_missing_literal(self) -> None:
        # 在美上市中国发行人：必须记录四项识别；缺失字段记“未获取到”，
        # 且不施加任何自动折价。
        valuation = self._engine_output(DCF, "valuation-vie-adr.json", "valuation")
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        vie_adr = plan["vie_adr"]
        self.assertTrue(vie_adr["us_listed_chinese_issuer"])
        self.assertEqual(vie_adr["adr_conversion_ratio"], 8)
        self.assertEqual(vie_adr["listing_regulator"], "SEC")
        self.assertEqual(vie_adr["delisting_or_conversion_risk"], "未获取到")
        self.assertEqual(vie_adr["vie_contract_control_risk"], "未获取到")
        self.assertNotIn("discount", json.dumps(vie_adr, ensure_ascii=False))

    def test_vie_adr_absent_or_flag_false_records_no_identification(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings, output_name="plan-a.json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("vie_adr", plan)

        def flag_false(payload: dict) -> None:
            payload["vie_adr"] = {"us_listed_chinese_issuer": False}

        valuation_false = self._engine_output_with_vie(flag_false)
        result, plan, _ = self._run_trade_plan(
            valuation_false,
            self._evidence_path(evidence, "evidence-b"),
            earnings,
            output_name="plan-b.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["vie_adr"], {"us_listed_chinese_issuer": False})

    def _engine_output_with_vie(self, mutate) -> dict:
        payload = json.loads(
            (FIXTURES / "valuation-vie-adr.json").read_text(encoding="utf-8")
        )
        payload["identity"] = dict(IDENTITY)
        mutate(payload)
        input_path = self.tmp / "valuation-vie-input.json"
        input_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output_path = self.tmp / "valuation-vie.json"
        result = _run(DCF, ["--input", str(input_path), "--output", str(output_path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output_path.read_text(encoding="utf-8"))

    def test_vie_adr_invalid_block_fails_closed(self) -> None:
        def non_boolean_flag(payload: dict) -> None:
            payload["vie_adr"]["us_listed_chinese_issuer"] = "yes"

        valuation = self._engine_output_with_vie(non_boolean_flag)
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, output_path = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("us_listed_chinese_issuer", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_evidence_identity_is_required_and_complete(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        for name, mutate in (
            ("missing", lambda evidence: evidence.pop("identity")),
            (
                "partial",
                lambda evidence: evidence["identity"].pop("schema_version"),
            ),
            (
                "wrong-version",
                lambda evidence: evidence["identity"].update(
                    {"artifact_version": 2}
                ),
            ),
        ):
            with self.subTest(case=name):
                evidence = self._fixture_evidence("technical-evidence-qualified.json")
                mutate(evidence)
                result, plan, output_path = self._run_trade_plan(
                    valuation,
                    self._evidence_path(evidence, f"evidence-{name}"),
                    earnings,
                    output_name=f"trade-plan-{name}.json",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("identity", result.stderr)
                self.assertIsNone(plan)
                self.assertFalse(output_path.exists())

    def test_input_schema_version_must_be_one(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        cases = []
        broken_valuation = dict(valuation, schema_version=2)
        cases.append(("valuation", broken_valuation, earnings, None))
        broken_earnings = dict(earnings, schema_version=2)
        cases.append(("earnings-quality", valuation, broken_earnings, None))
        cases.append(("thesis", valuation, earnings, {"schema_version": 2}))
        for name, valuation_input, earnings_input, thesis_override in cases:
            with self.subTest(input=name):
                result, plan, output_path = self._run_trade_plan(
                    valuation_input,
                    self._evidence_path(evidence, f"evidence-sv-{name}"),
                    earnings_input,
                    thesis_override=thesis_override,
                    output_name=f"trade-plan-sv-{name}.json",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema_version must be 1", result.stderr)
                self.assertIsNone(plan)
                self.assertFalse(output_path.exists())

    def test_input_identity_versions_must_be_one(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, output_path = self._run_trade_plan(
            valuation,
            self._evidence_path(evidence),
            earnings,
            thesis_override={
                "identity": {**IDENTITY, "artifact_version": 2}
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact_version must be 1", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_key_level_semantics_fail_closed(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        cases = (
            (
                "support-above-close",
                [{"side": "support", "price": 13.0}, {"side": "resistance", "price": 15.0}],
                "support must not be above",
            ),
            (
                "resistance-below-close",
                [{"side": "support", "price": 12.0}, {"side": "resistance", "price": 12.1}],
                "resistance must not be below",
            ),
            (
                "non-positive-price",
                [{"side": "support", "price": 0.0}, {"side": "resistance", "price": 15.0}],
                "must be positive",
            ),
        )
        for name, key_levels, expected in cases:
            with self.subTest(case=name):
                evidence = self._fixture_evidence("technical-evidence-qualified.json")
                evidence["key_levels"] = key_levels
                result, plan, output_path = self._run_trade_plan(
                    valuation,
                    self._evidence_path(evidence, f"evidence-kl-{name}"),
                    earnings,
                    output_name=f"trade-plan-kl-{name}.json",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIsNone(plan)
                self.assertFalse(output_path.exists())

    def test_evidence_identity_mismatch_fails_closed(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence["identity"] = {**IDENTITY, "case_id": "case-other-999"}
        result, plan, output_path = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("identity mismatch", result.stderr)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_epv_fallback_entry_plan_when_dcf_not_applicable(self) -> None:
        valuation = self._engine_output(DCF, "valuation-financial-epv.json", "valuation")
        self.assertEqual(valuation["results"]["dcf"]["status"], "not_applicable")
        anchor = valuation["results"]["epv"]["epv_per_share"]
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "entry_plan")
        self.assertTrue(plan["gates"]["valuation"]["pass"])
        margin = 1 - self.rules["safety_margin"]
        expected_band = {
            "low": round(anchor * (1 - self.rules["safety_margin"]) * margin, 6),
            "high": round(anchor * (1 + self.rules["safety_margin"]) * margin, 6),
        }
        self.assertEqual(plan["entry_plan"]["value_band"], expected_band)
        self.assertEqual(
            plan["target_plan"]["fundamental_target"]["level"], round(anchor, 6)
        )
        self.assertEqual(plan["target_plan"]["fundamental_target"]["basis"], "epv")
        self.assertIn("epv", plan["entry_plan"]["basis"])
        self.assertTrue(
            any("epv" in gap for gap in plan["data_gaps"]),
            plan["data_gaps"],
        )

    def test_no_applicable_valuation_model_watch_without_crash(self) -> None:
        payload = json.loads(
            (FIXTURES / "valuation-financial-epv.json").read_text(encoding="utf-8")
        )
        payload["identity"] = dict(IDENTITY)
        payload["models"]["epv"] = {
            "status": "not_applicable",
            "reason": "离线验收示例：全部估值模型均不适用。",
        }
        input_path = self.tmp / "valuation-na-input.json"
        input_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        engine = _run(
            DCF,
            ["--input", str(input_path), "--output", str(self.tmp / "valuation-na.json")],
        )
        self.assertEqual(engine.returncode, 0, engine.stderr)
        valuation = json.loads((self.tmp / "valuation-na.json").read_text(encoding="utf-8"))
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse(plan["gates"]["valuation"]["pass"])
        self.assertEqual(plan["veto"]["gate"], "valuation")
        self.assertEqual(
            set(plan["veto"].keys()), {"gate", "reason"}
        )
        self.assertIsNone(plan["entry_plan"]["zone"])
        self.assertIsNone(plan["downside_pct"])
        self.assertIsNone(plan["reward_risk_ratio"])
        what_would_change = plan["entry_plan"]["what_would_change"]
        self.assertTrue(what_would_change)
        self.assertTrue(
            all(isinstance(item, str) for item in what_would_change)
        )
        self.assertTrue(plan["data_gaps"])

    def test_html_escapes_symbol_special_characters(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        # symbol 与 identity listing_id 绑定：特殊字符符号需同步到全部输入身份。
        weird = 'A<B&"C'
        valuation["identity"]["listing_id"] = weird
        earnings["identity"]["listing_id"] = weird
        evidence["identity"]["listing_id"] = weird
        evidence["symbol"] = weird
        result, plan, _ = self._run_trade_plan(
            valuation,
            self._evidence_path(evidence),
            earnings,
            thesis_override={"identity": {**IDENTITY, "listing_id": weird}},
            html=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (self.tmp / "trade-plan.html").read_text(encoding="utf-8")
        self.assertNotIn('A<B&"C', html)
        self.assertIn("A&lt;B&amp;&quot;C", html)
        # 转义不破坏 SVG 结构。
        self.assertEqual(html.count("<svg"), 1)
        self.assertIn("</svg>", html)
        self.assertFalse(TRADE_DIRECTIVE.search(html))

    def test_watch_does_not_generate_html(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings("earnings-quality-c.json")
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, plan, _ = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings, html=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["status"], "watch")
        self.assertFalse((self.tmp / "trade-plan.html").exists())
        self.assertIn("watch", result.stderr)

    def test_output_has_no_trade_directives(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        result, _, output_path = self._run_trade_plan(
            valuation, self._evidence_path(evidence), earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = output_path.read_text(encoding="utf-8")
        self.assertIsNone(TRADE_DIRECTIVE.search(text))

    def test_existing_output_path_fails(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence_path = self._evidence_path(evidence)
        result, _, output_path = self._run_trade_plan(
            valuation, evidence_path, earnings
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        valuation_path = self.tmp / "valuation-in.json"
        earnings_path = self.tmp / "earnings-in.json"
        thesis_path = self.tmp / "thesis-in.json"
        again = _run(
            TRADE_PLAN,
            [
                "--valuation", str(valuation_path),
                "--evidence", str(evidence_path),
                "--earnings-quality", str(earnings_path),
                "--thesis", str(thesis_path),
                "--output", str(output_path),
            ],
        )
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("exists", again.stderr.lower())

    def test_output_inside_runtime_package_rejected(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        valuation_path = self._evidence_path(valuation, "valuation-in")
        earnings_path = self._evidence_path(earnings, "earnings-in")
        thesis_path = self._evidence_path(
            json.loads((FIXTURES / "thesis-pass.json").read_text(encoding="utf-8")),
            "thesis-in",
        )
        result = _run(
            TRADE_PLAN,
            [
                "--valuation", str(valuation_path),
                "--evidence", str(self._evidence_path(evidence)),
                "--earnings-quality", str(earnings_path),
                "--thesis", str(thesis_path),
                "--output", str(PACKAGE / "scripts" / "trade-plan.json"),
            ],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime package", result.stderr)

    def _expect_fail_closed(
        self,
        valuation: dict,
        evidence: dict,
        earnings: dict,
        expected: str,
        stem: str,
        thesis_override: dict | None = None,
    ) -> None:
        result, plan, output_path = self._run_trade_plan(
            valuation,
            self._evidence_path(evidence, f"evidence-{stem}"),
            earnings,
            thesis_override=thesis_override,
            output_name=f"trade-plan-{stem}.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIsNone(plan)
        self.assertFalse(output_path.exists())

    def test_evidence_top_level_schema_version_must_be_one(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        for name, version in (("two", 2), ("bool", True), ("missing", None)):
            with self.subTest(case=name):
                evidence = self._fixture_evidence("technical-evidence-qualified.json")
                if version is None:
                    evidence.pop("schema_version")
                else:
                    evidence["schema_version"] = version
                self._expect_fail_closed(
                    valuation,
                    evidence,
                    earnings,
                    "schema_version must be 1",
                    f"sv-{name}",
                )

    def test_evidence_unknown_status_fails_closed(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence["status"] = "stale"
        self._expect_fail_closed(
            valuation, evidence, earnings, "not a known status", "status"
        )

    def test_evidence_timeframe_must_be_daily(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence["timeframe"] = "1h"
        self._expect_fail_closed(
            valuation, evidence, earnings, "timeframe", "timeframe"
        )

    def test_evidence_symbol_must_match_listing(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence["symbol"] = "OTHER.US"
        self._expect_fail_closed(
            valuation, evidence, earnings, "symbol", "symbol"
        )

    def test_evidence_as_of_after_valuation_fails_closed(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        evidence["as_of"] = "2026-07-31T00:00:00Z"
        self._expect_fail_closed(
            valuation, evidence, earnings, "must not be after valuation", "as-of"
        )

    def test_earnings_grade_whitelist_and_veto_consistency(self) -> None:
        valuation = self._valuation()
        base_earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        cases = (
            ("bad-grade", {"grade": "E"}, "grade must be A, B, C, or D"),
            # 伪造 long_entry_veto=false 不能绕过 C 级否决。
            (
                "forged-veto-false",
                {"grade": "C", "long_entry_veto": False},
                "contradicts grade",
            ),
            (
                "forged-veto-true",
                {"grade": "A", "long_entry_veto": True},
                "contradicts grade",
            ),
        )
        for name, mutation, expected in cases:
            with self.subTest(case=name):
                earnings = json.loads(json.dumps(base_earnings))
                earnings.update(mutation)
                self._expect_fail_closed(
                    valuation, evidence, earnings, expected, f"eq-{name}"
                )

    def test_market_scope_and_currency_whitelists(self) -> None:
        base_valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        cases = (
            ("market-scope", {"market_scope": "crypto"}, "market_scope"),
            ("currency", {"currency": "EUR"}, "currency"),
        )
        for name, mutation, expected in cases:
            with self.subTest(case=name):
                valuation = json.loads(json.dumps(base_valuation))
                valuation.update(mutation)
                self._expect_fail_closed(
                    valuation, evidence, earnings, expected, f"scope-{name}"
                )

    def test_short_directive_rejected(self) -> None:
        valuation = self._valuation()
        earnings = self._earnings()
        evidence = self._fixture_evidence("technical-evidence-qualified.json")
        for name, statement in (
            ("zh", "建议做空该公司。"),
            ("en", "Consider a short position here."),
        ):
            with self.subTest(case=name):
                self._expect_fail_closed(
                    valuation,
                    evidence,
                    earnings,
                    "trade directive",
                    f"short-{name}",
                    thesis_override={"preregistered_hypothesis": statement},
                )


if __name__ == "__main__":
    unittest.main()
