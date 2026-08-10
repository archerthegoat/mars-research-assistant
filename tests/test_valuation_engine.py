from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Callable
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "skills" / "mars-research-assistant"
VALUATION_ENGINE = (
    RUNTIME / "skills" / "deep-equity-research" / "scripts" / "dcf.py"
)
FIXTURES = ROOT / "tests" / "fixtures"
FULL_FIXTURE = FIXTURES / "valuation-full.json"
MISSING_FIXTURE = FIXTURES / "valuation-missing-inputs.json"
INVALID_PROBABILITY_FIXTURE = FIXTURES / "valuation-invalid-probability.json"
NOT_APPLICABLE_FIXTURE = FIXTURES / "valuation-not-applicable.json"


def scenario_per_share(
    cash_flows: list[float],
    wacc: float,
    terminal_growth: float,
    net_debt: float,
    shares: float,
) -> float:
    discounted = sum(
        cash_flow / (1 + wacc) ** year
        for year, cash_flow in enumerate(cash_flows, 1)
    )
    terminal = cash_flows[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    enterprise = discounted + terminal / (1 + wacc) ** len(cash_flows)
    return (enterprise - net_debt) / shares


class ValuationEngineTests(unittest.TestCase):
    def _run(self, fixture: Path, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VALUATION_ENGINE), "--input", str(fixture), "--output", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_full_fixture_recomputes_all_models_deterministically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            first_output = Path(temporary) / "mars-research" / "valuation-1.json"
            second_output = Path(temporary) / "mars-research" / "valuation-2.json"
            first = self._run(FULL_FIXTURE, first_output)
            second = self._run(FULL_FIXTURE, second_output)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            artifact = json.loads(first_output.read_text(encoding="utf-8"))
            rerun = json.loads(second_output.read_text(encoding="utf-8"))
        self.assertEqual(artifact, rerun)
        self.assertEqual(artifact["engine_version"], "1.0.0")
        self.assertEqual(artifact["model_version"], "v1.0.3-valuation-1")
        self.assertEqual(artifact["market_scope"], "us")
        self.assertEqual(artifact["currency"], "USD")
        for field in ("issuer_id", "listing_id", "case_id", "artifact_version", "schema_version"):
            self.assertIn(field, artifact["identity"])

        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        dcf_input = fixture["models"]["dcf"]
        wacc = dcf_input["wacc"]["value"]
        growth = dcf_input["terminal_growth"]["value"]
        net_debt = dcf_input["net_debt"]["value"]
        shares = dcf_input["shares_outstanding"]["value"]
        input_scenarios = {
            item["name"]: item for item in dcf_input["scenarios"]
        }
        results = artifact["results"]
        for model in ("dcf", "reverse_dcf", "pvgo", "epv", "eva", "sotp", "monte_carlo"):
            with self.subTest(model=model):
                self.assertEqual(results[model]["status"], "computed")

        dcf = results["dcf"]
        self.assertEqual(
            [entry["name"] for entry in dcf["scenarios"]], ["bear", "base", "bull"]
        )
        weighted = 0.0
        per_share_by_name: dict[str, float] = {}
        for entry in dcf["scenarios"]:
            source = input_scenarios[entry["name"]]
            expected = scenario_per_share(
                source["free_cash_flows"], wacc, growth, net_debt, shares
            )
            per_share_by_name[entry["name"]] = expected
            self.assertAlmostEqual(entry["per_share"], expected, delta=1e-6)
            self.assertAlmostEqual(
                entry["probability"], source["probability"]["value"], delta=1e-12
            )
            weighted += source["probability"]["value"] * expected
        self.assertAlmostEqual(
            dcf["probability_weighted_per_share"], weighted, delta=1e-6
        )
        checks = dcf["terminal_value_checks"]
        for key in ("long_run_growth", "mature_margin", "reinvestment_roic_consistency"):
            with self.subTest(check=key):
                self.assertIn(key, checks)
                self.assertIn(checks[key]["status"], {"pass", "warn", "fail"})
                self.assertIsInstance(checks[key]["detail"], str)
        self.assertEqual(checks["long_run_growth"]["status"], "pass")
        self.assertEqual(checks["mature_margin"]["status"], "pass")
        self.assertEqual(checks["reinvestment_roic_consistency"]["status"], "pass")
        self.assertAlmostEqual(
            dcf["value_zone"]["low"], per_share_by_name["bear"], delta=1e-6
        )
        self.assertAlmostEqual(dcf["value_zone"]["high"], weighted, delta=1e-6)

        monte_carlo = results["monte_carlo"]
        self.assertEqual(monte_carlo["seed"], 42)
        self.assertEqual(monte_carlo["trials"], 500)
        self.assertEqual(
            monte_carlo, rerun["results"]["monte_carlo"],
            "seeded Monte Carlo must be byte-identical across runs",
        )
        for key in ("p10", "p50", "p90"):
            self.assertIn(key, monte_carlo["percentiles"])
        self.assertLessEqual(
            monte_carlo["percentiles"]["p10"], monte_carlo["percentiles"]["p50"]
        )
        self.assertLessEqual(
            monte_carlo["percentiles"]["p50"], monte_carlo["percentiles"]["p90"]
        )
        self.assertEqual(results["reverse_dcf"]["horizon_years"], 10)

    def test_missing_inputs_fail_closed_without_fair_value_numbers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            output = Path(temporary) / "mars-research" / "valuation.json"
            result = self._run(MISSING_FIXTURE, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        dcf = artifact["results"]["dcf"]
        self.assertEqual(dcf["status"], "missing_inputs")
        self.assertIn("wacc", dcf["missing"])
        self.assertNotIn("per_share", json.dumps(dcf, ensure_ascii=False))
        self.assertTrue(artifact["data_gaps"])

    def test_probabilities_not_summing_to_one_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            output = Path(temporary) / "mars-research" / "valuation.json"
            result = self._run(INVALID_PROBABILITY_FIXTURE, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        dcf = artifact["results"]["dcf"]
        self.assertEqual(dcf["status"], "invalid_inputs")
        self.assertNotIn("probability_weighted_per_share", dcf)
        self.assertTrue(
            any("概率" in gap for gap in artifact["data_gaps"]),
            artifact["data_gaps"],
        )

    def test_not_applicable_models_keep_their_reason(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            output = Path(temporary) / "mars-research" / "valuation.json"
            result = self._run(NOT_APPLICABLE_FIXTURE, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        fixture = json.loads(NOT_APPLICABLE_FIXTURE.read_text(encoding="utf-8"))
        for model in ("pvgo", "epv"):
            with self.subTest(model=model):
                entry = artifact["results"][model]
                self.assertEqual(entry["status"], "not_applicable")
                self.assertEqual(entry["reason"], fixture["models"][model]["reason"])
        self.assertEqual(artifact["results"]["dcf"]["status"], "computed")

    def test_reverse_dcf_reports_no_solution_for_extreme_price(self) -> None:
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        fixture["models"]["dcf"]["price"]["value"] = 10_000_000.0
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            temporary_path = Path(temporary)
            extreme = temporary_path / "extreme-price.json"
            extreme.write_text(
                json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
            )
            output = temporary_path / "mars-research" / "valuation.json"
            result = self._run(extreme, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        reverse = artifact["results"]["reverse_dcf"]
        self.assertEqual(reverse["status"], "no_solution")
        self.assertNotIn("implied_fcf_cagr", reverse)
        self.assertIn("-95.00%", reverse["detail"])
        self.assertIn("300.00%", reverse["detail"])

    def test_output_path_inside_runtime_package_is_refused(self) -> None:
        output = RUNTIME / "blocked-valuation.json"
        try:
            result = self._run(FULL_FIXTURE, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Skill runtime package", result.stderr)
            self.assertFalse(output.exists())
        finally:
            output.unlink(missing_ok=True)

    def test_output_never_overwrites_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            output = Path(temporary) / "mars-research" / "valuation.json"
            first = self._run(FULL_FIXTURE, output)
            second = self._run(FULL_FIXTURE, output)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("File exists", second.stderr)

    def _run_modified_fixture(
        self, mutate: Callable[[dict], None]
    ) -> dict:
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        mutate(fixture)
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            temporary_path = Path(temporary)
            modified = temporary_path / "modified.json"
            modified.write_text(
                json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
            )
            output = temporary_path / "mars-research" / "valuation.json"
            result = self._run(modified, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(output.read_text(encoding="utf-8"))

    def test_provenance_retains_source_derivation_currency_and_period(self) -> None:
        def mutate(fixture: dict) -> None:
            fixture["models"]["dcf"]["net_debt"]["currency"] = "USD"
            fixture["models"]["dcf"]["net_debt"]["accounting_period"] = "FY2025"
            fixture["models"]["monte_carlo"]["distributions"]["wacc"]["source"] = {
                "name": "Example distribution assumption",
                "kind": "valuation_assumption",
                "as_of": "2026-07-30T00:00:00Z",
                "url": "https://example.com/distribution",
            }

        artifact = self._run_modified_fixture(mutate)
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        dcf = artifact["results"]["dcf"]
        self.assertEqual(dcf["status"], "computed")
        self.assertNotIn("source_gaps", dcf)
        provenance = dcf["inputs_provenance"]
        for name in (
            "price",
            "shares_outstanding",
            "net_debt",
            "wacc",
            "terminal_growth",
            "long_run_growth_cap",
            "mature_margin_benchmark",
        ):
            with self.subTest(field=name):
                record = provenance[name]
                self.assertEqual(
                    record["value"], fixture["models"]["dcf"][name]["value"]
                )
                self.assertEqual(
                    record["source"], fixture["models"]["dcf"][name]["source"]
                )
        self.assertEqual(
            provenance["wacc"]["derivation"],
            fixture["models"]["dcf"]["wacc"]["derivation"],
        )
        self.assertEqual(provenance["net_debt"]["currency"], "USD")
        self.assertEqual(provenance["net_debt"]["accounting_period"], "FY2025")
        self.assertIsNone(provenance["price"]["currency"])
        self.assertIsNone(provenance["price"]["derivation"])
        monte_carlo = artifact["results"]["monte_carlo"]["inputs_provenance"]
        self.assertEqual(
            monte_carlo["distributions"]["wacc"]["source"]["name"],
            "Example distribution assumption",
        )
        self.assertIsNone(
            monte_carlo["distributions"]["terminal_growth"]["source"]
        )
        eva_nopat = artifact["results"]["eva"]["inputs_provenance"]["nopat_path"]
        self.assertEqual(eva_nopat["value"], fixture["models"]["eva"]["nopat_path"])
        self.assertIsNone(eva_nopat["source"])
        self.assertNotIn("source_inherited_from", eva_nopat)
        self.assertEqual(artifact["data_gaps"], [])

    def test_scenario_and_segment_source_inheritance(self) -> None:
        artifact = self._run_modified_fixture(lambda fixture: None)
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        scenarios = artifact["results"]["dcf"]["inputs_provenance"]["scenarios"]
        for name in ("bear", "base", "bull"):
            with self.subTest(scenario=name):
                scenario_source = next(
                    item["source"]
                    for item in fixture["models"]["dcf"]["scenarios"]
                    if item["name"] == name
                )
                record = scenarios[name]
                for field in (
                    "probability",
                    "free_cash_flows",
                    "margins",
                    "reinvestment_rate",
                    "roic",
                ):
                    self.assertEqual(record[field]["source"], scenario_source)
                    self.assertEqual(
                        record[field]["source_inherited_from"], "scenario"
                    )
        segments = artifact["results"]["sotp"]["inputs_provenance"]["segments"]
        self.assertEqual(
            [segment["name"] for segment in segments],
            ["core", "services", "investments"],
        )
        core = segments[0]["inputs"]
        self.assertEqual(
            core["wacc"]["source"],
            fixture["models"]["sotp"]["segments"][0]["source"],
        )
        self.assertEqual(core["wacc"]["source_inherited_from"], "segment")
        self.assertEqual(
            core["free_cash_flows"]["source_inherited_from"], "segment"
        )

        def mutate(fixture: dict) -> None:
            fixture["models"]["sotp"]["segments"][0]["inputs"]["wacc"]["source"] = {
                "name": "Segment-specific wacc note",
                "kind": "issuer_ir",
                "as_of": "2026-07-29T00:00:00Z",
                "url": "https://example.com/segment-wacc",
            }

        overridden = self._run_modified_fixture(mutate)
        core_wacc = overridden["results"]["sotp"]["inputs_provenance"]["segments"][0][
            "inputs"
        ]["wacc"]
        self.assertEqual(core_wacc["source"]["name"], "Segment-specific wacc note")
        self.assertNotIn("source_inherited_from", core_wacc)

    def test_missing_key_source_is_annotated_not_silent(self) -> None:
        def mutate(fixture: dict) -> None:
            del fixture["models"]["dcf"]["wacc"]["source"]
            del fixture["models"]["dcf"]["scenarios"][0]["source"]

        artifact = self._run_modified_fixture(mutate)
        dcf = artifact["results"]["dcf"]
        self.assertEqual(dcf["status"], "computed")
        self.assertIn("probability_weighted_per_share", dcf)
        self.assertEqual(
            dcf["source_gaps"], ["wacc", "scenarios[0].probability"]
        )
        self.assertIsNone(
            dcf["inputs_provenance"]["wacc"]["source"]
        )
        self.assertIsNone(
            dcf["inputs_provenance"]["scenarios"]["bear"]["probability"]["source"]
        )
        self.assertTrue(
            any("wacc" in gap and "缺少来源" in gap for gap in artifact["data_gaps"]),
            artifact["data_gaps"],
        )
        self.assertTrue(
            any(
                "scenarios[0].probability" in gap for gap in artifact["data_gaps"]
            ),
            artifact["data_gaps"],
        )

    def test_malformed_source_fails_closed(self) -> None:
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        fixture["models"]["dcf"]["price"]["source"]["kind"] = "search_summary"
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            temporary_path = Path(temporary)
            modified = temporary_path / "bad-source.json"
            modified.write_text(
                json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
            )
            output = temporary_path / "mars-research" / "valuation.json"
            result = self._run(modified, output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kind", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertFalse(output.exists())

    def _run_mutated_fixture(
        self, mutate: Callable[[dict], None]
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        mutate(fixture)
        temporary = tempfile.TemporaryDirectory(prefix="mars-v103-valuation-")
        self.addCleanup(temporary.cleanup)
        temporary_path = Path(temporary.name)
        modified = temporary_path / "modified.json"
        modified.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        output = temporary_path / "mars-research" / "valuation.json"
        return self._run(modified, output), output

    def test_identity_versions_must_be_one(self) -> None:
        for field in ("artifact_version", "schema_version"):
            with self.subTest(field=field):
                result, output = self._run_mutated_fixture(
                    lambda fixture: fixture["identity"].update({field: 2})
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"identity {field} must be 1", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertFalse(output.exists())

    def test_scenario_probability_out_of_range_is_rejected(self) -> None:
        # 概率之和仍为 1，但单情景概率越界 [0, 1] 必须拒绝。
        def mutate(fixture: dict) -> None:
            scenarios = fixture["models"]["dcf"]["scenarios"]
            scenarios[0]["probability"]["value"] = -0.25
            scenarios[1]["probability"]["value"] = 1.0

        result, output = self._run_mutated_fixture(mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        dcf = artifact["results"]["dcf"]
        self.assertEqual(dcf["status"], "invalid_inputs")
        self.assertIn("[0, 1]", dcf["detail"])
        self.assertNotIn("probability_weighted_per_share", dcf)

    def test_ah_compare_and_vie_adr_blocks_pass_through(self) -> None:
        def mutate(fixture: dict) -> None:
            fixture["ah_compare"] = {
                "pair": {"a_share_listing_id": "TP.SS", "hk_listing_id": "TP.HK"},
                "fx_pair": "CNY/HKD",
                "fx_rate": 1.09,
                "share_right_ratio": 1.0,
                "liquidity_diff": "离线验收示例：流动性差异。",
                "trading_day_diff": "离线验收示例：交易日历差异。",
                "premium_discount": 0.12,
            }
            fixture["vie_adr"] = {"us_listed_chinese_issuer": False}

        artifact = self._run_modified_fixture(mutate)
        self.assertEqual(artifact["ah_compare"]["fx_pair"], "CNY/HKD")
        self.assertEqual(artifact["ah_compare"]["fx_rate"], 1.09)
        self.assertEqual(artifact["vie_adr"], {"us_listed_chinese_issuer": False})

    def test_malformed_ah_compare_block_fails_closed(self) -> None:
        result, output = self._run_mutated_fixture(
            lambda fixture: fixture.update({"ah_compare": "not-an-object"})
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ah_compare requires an object", result.stderr)
        self.assertFalse(output.exists())

    def test_top_level_schema_version_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-v103-valuation-") as temporary:
            output = Path(temporary) / "mars-research" / "valuation.json"
            result = self._run(FULL_FIXTURE, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(artifact["schema_version"], 1)
        for field in ("issuer_id", "listing_id", "case_id", "artifact_version", "schema_version"):
            self.assertIn(field, artifact["identity"])
        self.assertEqual(artifact["identity"]["schema_version"], 1)

    def test_source_missing_url_fails_closed(self) -> None:
        def strip_dcf_url(fixture: dict) -> None:
            del fixture["models"]["dcf"]["price"]["source"]["url"]

        def strip_reverse_dcf_url(fixture: dict) -> None:
            del fixture["models"]["reverse_dcf"]["current_free_cash_flow"]["source"]["url"]

        def strip_monte_carlo_url(fixture: dict) -> None:
            fixture["models"]["monte_carlo"]["distributions"]["wacc"]["source"] = {
                "name": "Distribution assumption without url",
                "kind": "valuation_assumption",
                "as_of": "2026-07-30T00:00:00Z",
            }

        cases = {
            "dcf": strip_dcf_url,
            "reverse_dcf": strip_reverse_dcf_url,
            "monte_carlo": strip_monte_carlo_url,
        }
        for label, mutate in cases.items():
            with self.subTest(model=label):
                fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
                mutate(fixture)
                with tempfile.TemporaryDirectory(
                    prefix="mars-v103-valuation-"
                ) as temporary:
                    temporary_path = Path(temporary)
                    modified = temporary_path / "missing-url.json"
                    modified.write_text(
                        json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
                    )
                    output = temporary_path / "mars-research" / "valuation.json"
                    result = self._run(modified, output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("url", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertFalse(output.exists())


    def test_source_as_of_after_computed_as_of_fails_closed(self) -> None:
        cases = {
            "dcf price source": lambda fixture: fixture["models"]["dcf"]["price"][
                "source"
            ].update(as_of="2026-07-31T00:00:00Z"),
            "scenario source": lambda fixture: fixture["models"]["dcf"]["scenarios"][
                0
            ]["source"].update(as_of="2026-08-01T00:00:00Z"),
            "monte_carlo distribution source": lambda fixture: fixture["models"][
                "monte_carlo"
            ]["distributions"]["wacc"].update(
                source={
                    "name": "Late distribution assumption",
                    "kind": "valuation_assumption",
                    "as_of": "2026-07-30T12:00:01Z",
                    "url": "https://example.com/late-distribution",
                }
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                result, output = self._run_mutated_fixture(mutate)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("after fixture computed_as_of", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertFalse(output.exists())

    def test_bool_top_level_schema_version_fails_closed(self) -> None:
        result, output = self._run_mutated_fixture(
            lambda fixture: fixture.update(schema_version=True)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture schema_version must be 1", result.stderr)
        self.assertFalse(output.exists())

    def test_unknown_market_scope_and_currency_fail_closed(self) -> None:
        for field, value in (("market_scope", "moon"), ("currency", "JPY")):
            with self.subTest(field=field):
                result, output = self._run_mutated_fixture(
                    lambda fixture, f=field, v=value: fixture.update({f: v})
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not supported", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertFalse(output.exists())

    def test_short_selling_advice_in_guarded_text_fails_closed(self) -> None:
        cases = {
            "derivation 做空": lambda fixture: fixture["models"]["dcf"]["wacc"].update(
                derivation="建议做空对冲。"
            ),
            "derivation short": lambda fixture: fixture["models"]["dcf"]["wacc"].update(
                derivation="pair with a short leg"
            ),
            "rationale 沽空": lambda fixture: fixture["models"]["dcf"]["scenarios"][
                0
            ]["probability"].update(rationale="可沽空相关 ETF 对冲。"),
            "not_applicable reason 卖空": lambda fixture: fixture["models"][
                "pvgo"
            ].update(status="not_applicable", reason="建议卖空。"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                result, output = self._run_mutated_fixture(mutate)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("trade directive", result.stderr)
                self.assertFalse(output.exists())

    def test_pe_multiple_computes_forward_eps_reference(self) -> None:
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        source = {
            "name": "Example PE assumption",
            "kind": "valuation_assumption",
            "as_of": "2026-07-30T00:00:00Z",
            "url": "https://example.com/pe",
        }
        fixture["models"]["pe"] = {
            "status": "requested",
            "price": {
                "value": 100.0,
                "source": {
                    "name": "Example public quote",
                    "kind": "public_quote",
                    "as_of": "2026-07-30T00:00:00Z",
                    "url": "https://example.com/quote",
                },
            },
            "earnings_basis": "forward_eps",
            "basis_rationale": "离线验收示例：下一财年 EPS 作为前瞻口径。",
            "scenarios": [
                {
                    "name": "bear",
                    "probability": {"value": 0.25, "rationale": "保守盈利情景。", "source": source},
                    "eps": {"value": 4.0, "source": source},
                    "pe_multiple": {"value": 15.0, "source": source},
                },
                {
                    "name": "base",
                    "probability": {"value": 0.5, "rationale": "基准盈利情景。", "source": source},
                    "eps": {"value": 5.0, "source": source},
                    "pe_multiple": {"value": 20.0, "source": source},
                },
                {
                    "name": "bull",
                    "probability": {"value": 0.25, "rationale": "乐观盈利情景。", "source": source},
                    "eps": {"value": 6.0, "source": source},
                    "pe_multiple": {"value": 25.0, "source": source},
                },
            ],
        }
        with tempfile.TemporaryDirectory(prefix="mars-v103-pe-") as temporary:
            fixture_path = Path(temporary) / "pe-input.json"
            output = Path(temporary) / "mars-research" / "valuation.json"
            fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            result = self._run(fixture_path, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        pe = artifact["results"]["pe"]
        self.assertEqual(pe["status"], "computed")
        self.assertEqual(pe["quality"]["status"], "usable")
        self.assertAlmostEqual(pe["probability_weighted_eps"], 5.0, delta=1e-6)
        self.assertAlmostEqual(pe["probability_weighted_per_share"], 102.5, delta=1e-6)
        self.assertAlmostEqual(pe["value_zone"]["low"], 60.0, delta=1e-6)
        self.assertAlmostEqual(pe["current_pe"], 20.0, delta=1e-6)

    def test_pe_trailing_eps_is_conditional_and_non_positive_eps_fails_closed(self) -> None:
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        source = {
            "name": "Example PE assumption",
            "kind": "valuation_assumption",
            "as_of": "2026-07-30T00:00:00Z",
            "url": "https://example.com/pe",
        }
        fixture["models"]["pe"] = {
            "status": "requested",
            "price": {"value": 100.0, "source": source},
            "earnings_basis": "trailing_eps",
            "basis_rationale": "离线验收示例：历史 EPS，需复核一次性项目。",
            "scenarios": [
                {
                    "name": name,
                    "probability": {"value": probability, "rationale": f"{name} 情景。"},
                    "eps": {"value": eps, "source": source},
                    "pe_multiple": {"value": multiple, "source": source},
                }
                for name, probability, eps, multiple in (
                    ("bear", 0.25, 4.0, 15.0),
                    ("base", 0.5, 5.0, 20.0),
                    ("bull", 0.25, 6.0, 25.0),
                )
            ],
        }
        with tempfile.TemporaryDirectory(prefix="mars-v103-pe-") as temporary:
            fixture_path = Path(temporary) / "pe-input.json"
            output = Path(temporary) / "mars-research" / "valuation.json"
            fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            result = self._run(fixture_path, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(artifact["results"]["pe"]["quality"]["status"], "conditional")

        fixture["models"]["pe"]["scenarios"][1]["eps"]["value"] = 0.0
        with tempfile.TemporaryDirectory(prefix="mars-v103-pe-") as temporary:
            fixture_path = Path(temporary) / "pe-input.json"
            output = Path(temporary) / "mars-research" / "valuation.json"
            fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            result = self._run(fixture_path, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        pe = artifact["results"]["pe"]
        self.assertEqual(pe["status"], "invalid_inputs")
        self.assertNotIn("probability_weighted_per_share", pe)


DRIVER_SOURCE = {
    "name": "Example driver assumption",
    "kind": "valuation_assumption",
    "as_of": "2026-07-30T00:00:00Z",
    "url": "https://example.com/driver",
}


def driver_model_payload() -> dict:
    """Generic two-scenario driver model; no relation to any real issuer."""
    base_arrays = {
        "revenue": [1000.0, 1100.0, 1200.0, 1300.0, 1400.0],
        "operating_margin": [0.12, 0.13, 0.14, 0.15, 0.16],
        "tax_rate": [0.25, 0.25, 0.25, 0.25, 0.25],
        "depreciation_amortization": [30.0, 30.0, 30.0, 30.0, 30.0],
        "capex": [50.0, 50.0, 50.0, 50.0, 50.0],
        "change_in_nwc": [10.0, 10.0, 10.0, 10.0, 10.0],
    }
    bull_arrays = {
        "revenue": [1000.0, 1150.0, 1300.0, 1450.0, 1600.0],
        "operating_margin": [0.13, 0.14, 0.15, 0.16, 0.17],
        "tax_rate": [0.25, 0.25, 0.25, 0.25, 0.25],
        "depreciation_amortization": [30.0, 30.0, 30.0, 30.0, 30.0],
        "capex": [55.0, 55.0, 55.0, 55.0, 55.0],
        "change_in_nwc": [12.0, 12.0, 12.0, 12.0, 12.0],
    }
    return {
        "scenarios": [
            {
                "name": "conservative",
                "probability": {"value": 0.5, "rationale": "离线验收示例：保守情景概率。"},
                "forecast_periods": 5,
                "reinvestment_rate": {"value": 0.3},
                "roic": {"value": 0.1},
                "source": dict(DRIVER_SOURCE),
                **base_arrays,
            },
            {
                "name": "optimistic",
                "probability": {"value": 0.5, "rationale": "离线验收示例：乐观情景概率。"},
                "forecast_periods": 5,
                "reinvestment_rate": {"value": 0.3},
                "roic": {"value": 0.1},
                "source": dict(DRIVER_SOURCE),
                **bull_arrays,
            },
        ]
    }


def driver_fcf_path(scenario: dict) -> list[float]:
    """Reference implementation of NOPAT/FCF derivation for assertions."""
    periods = scenario["forecast_periods"]
    nopat = [
        scenario["revenue"][year]
        * scenario["operating_margin"][year]
        * (1 - scenario["tax_rate"][year])
        for year in range(periods)
    ]
    return [
        nopat[year]
        + scenario["depreciation_amortization"][year]
        - scenario["capex"][year]
        - scenario["change_in_nwc"][year]
        for year in range(periods)
    ]


class DriverDcfTests(unittest.TestCase):
    def _run_with_driver(
        self, mutate_driver: Callable[[dict], None] | None = None,
        mutate_fixture: Callable[[dict], None] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        driver = driver_model_payload()
        if mutate_driver is not None:
            mutate_driver(driver)
        fixture["models"]["dcf"]["driver_model"] = driver
        if mutate_fixture is not None:
            mutate_fixture(fixture)
        temporary = tempfile.TemporaryDirectory(prefix="mars-v103-driver-")
        self.addCleanup(temporary.cleanup)
        temporary_path = Path(temporary.name)
        modified = temporary_path / "modified.json"
        modified.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        output = temporary_path / "mars-research" / "valuation.json"
        return self._run_engine(modified, output), output

    def _run_engine(self, fixture: Path, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VALUATION_ENGINE), "--input", str(fixture), "--output", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_driver_dcf_derives_fcf_and_values(self) -> None:
        result, output = self._run_with_driver()
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        fixture = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
        dcf_input = fixture["models"]["dcf"]
        wacc = dcf_input["wacc"]["value"]
        growth = dcf_input["terminal_growth"]["value"]
        net_debt = dcf_input["net_debt"]["value"]
        shares = dcf_input["shares_outstanding"]["value"]

        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["model_kind"], "driver_dcf")
        self.assertEqual(driver["model_version"], "v1.0.3-valuation-1")
        self.assertEqual(driver["status"], "computed")
        self.assertEqual(driver["quality"]["status"], "usable")
        self.assertEqual(driver["quality"]["flags"], [])
        payload = driver_model_payload()
        weighted = 0.0
        per_shares = []
        for entry, source in zip(driver["scenarios"], payload["scenarios"]):
            with self.subTest(scenario=entry["name"]):
                self.assertEqual(entry["name"], source["name"])
                expected_fcf = driver_fcf_path(source)
                self.assertEqual(entry["forecast_periods"], source["forecast_periods"])
                self.assertEqual(entry["drivers"]["revenue"], source["revenue"])
                for actual, expected in zip(entry["free_cash_flows"], expected_fcf):
                    self.assertAlmostEqual(actual, expected, delta=1e-6)
                expected_per_share = scenario_per_share(
                    expected_fcf, wacc, growth, net_debt, shares
                )
                per_shares.append(expected_per_share)
                self.assertAlmostEqual(entry["per_share"], expected_per_share, delta=1e-6)
                self.assertIn("terminal_value", entry)
                self.assertIn("terminal_value_share_of_enterprise", entry)
                weighted += source["probability"]["value"] * expected_per_share
        self.assertAlmostEqual(
            driver["probability_weighted_per_share"], weighted, delta=1e-6
        )
        self.assertAlmostEqual(driver["value_zone"]["low"], min(per_shares), delta=1e-6)
        self.assertAlmostEqual(driver["value_zone"]["high"], weighted, delta=1e-6)
        for key in ("long_run_growth", "mature_margin", "reinvestment_roic_consistency"):
            self.assertEqual(driver["terminal_value_checks"][key]["status"], "pass")
        self.assertEqual(
            driver["terminal_assumptions"]["terminal_growth"], growth
        )
        self.assertEqual(driver["terminal_assumptions"]["wacc"], wacc)
        # 旧 DCF 保持 baseline 兼容：数值不变，仅加角色标记。
        baseline = artifact["results"]["dcf"]
        self.assertEqual(baseline["model_role"], "baseline")
        self.assertIn("probability_weighted_per_share", baseline)
        self.assertEqual(artifact["data_gaps"], [])

    def test_driver_model_absent_keeps_legacy_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-v103-driver-") as temporary:
            output = Path(temporary) / "valuation.json"
            result = self._run_engine(FULL_FIXTURE, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
        self.assertNotIn("driver_dcf", artifact["results"])
        self.assertEqual(artifact["results"]["dcf"]["model_role"], "baseline")

    def test_driver_dcf_missing_field_fails_closed(self) -> None:
        def mutate(driver: dict) -> None:
            del driver["scenarios"][1]["capex"]

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["status"], "missing_inputs")
        self.assertIn("scenarios[1].capex", driver["missing"])
        self.assertEqual(driver["quality"]["status"], "unreliable")
        self.assertNotIn("per_share", json.dumps(driver, ensure_ascii=False))

    def test_driver_dcf_requires_reinvestment_and_roic(self) -> None:
        for field in ("reinvestment_rate", "roic"):
            with self.subTest(field=field):
                def mutate(driver: dict, field: str = field) -> None:
                    del driver["scenarios"][0][field]

                result, output = self._run_with_driver(mutate_driver=mutate)
                self.assertEqual(result.returncode, 0, result.stderr)
                artifact = json.loads(output.read_text(encoding="utf-8"))
                driver = artifact["results"]["driver_dcf"]
                self.assertEqual(driver["status"], "missing_inputs")
                self.assertIn(f"scenarios[0].{field}", driver["missing"])
                self.assertEqual(driver["quality"]["status"], "unreliable")

    def test_driver_dcf_array_length_mismatch_fails_closed(self) -> None:
        def mutate(driver: dict) -> None:
            driver["scenarios"][0]["capex"] = [50.0, 50.0]

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forecast_periods", result.stderr)
        self.assertFalse(output.exists())

    def test_driver_dcf_non_finite_value_fails_closed(self) -> None:
        def mutate(driver: dict) -> None:
            driver["scenarios"][0]["revenue"][2] = 1e999

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("finite", result.stderr)
        self.assertFalse(output.exists())

    def test_driver_dcf_negative_revenue_is_invalid(self) -> None:
        def mutate(driver: dict) -> None:
            driver["scenarios"][0]["revenue"][0] = -5.0

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["status"], "invalid_inputs")
        self.assertIn("revenue", driver["detail"])
        self.assertEqual(driver["quality"]["status"], "unreliable")
        self.assertNotIn("probability_weighted_per_share", driver)

    def test_driver_dcf_probability_sum_is_enforced(self) -> None:
        def mutate(driver: dict) -> None:
            driver["scenarios"][0]["probability"]["value"] = 0.7

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["status"], "invalid_inputs")
        self.assertIn("概率", driver["detail"])

    def test_driver_dcf_short_horizon_is_conditional(self) -> None:
        def mutate(driver: dict) -> None:
            for scenario in driver["scenarios"]:
                scenario["forecast_periods"] = 3
                for field in (
                    "revenue",
                    "operating_margin",
                    "tax_rate",
                    "depreciation_amortization",
                    "capex",
                    "change_in_nwc",
                ):
                    scenario[field] = scenario[field][:3]

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["status"], "computed")
        self.assertEqual(driver["quality"]["status"], "conditional")
        self.assertIn("short_forecast_horizon", driver["quality"]["flags"])
        self.assertTrue(
            any(
                "conditional" in gap and "未形成基本面目标" in gap
                for gap in artifact["data_gaps"]
            ),
            artifact["data_gaps"],
        )

    def test_driver_dcf_terminal_check_fail_is_unreliable(self) -> None:
        def mutate(driver: dict) -> None:
            # 终值年经营利润率远超成熟利润率基准 → mature_margin fail。
            for scenario in driver["scenarios"]:
                scenario["operating_margin"][-1] = 0.5

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["status"], "computed")
        self.assertEqual(
            driver["terminal_value_checks"]["mature_margin"]["status"], "fail"
        )
        self.assertEqual(driver["quality"]["status"], "unreliable")
        self.assertIn("terminal_check_fail:mature_margin", driver["quality"]["flags"])
        self.assertTrue(
            any(
                "unreliable" in gap and "未形成基本面目标" in gap
                for gap in artifact["data_gaps"]
            ),
            artifact["data_gaps"],
        )

    def test_driver_dcf_missing_or_stale_key_source_is_conditional(self) -> None:
        def drop_source(fixture: dict) -> None:
            del fixture["models"]["dcf"]["shares_outstanding"]["source"]

        result, output = self._run_with_driver(mutate_fixture=drop_source)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["quality"]["status"], "conditional")
        self.assertIn(
            "key_source_missing:shares_outstanding", driver["quality"]["flags"]
        )

        def stale_source(fixture: dict) -> None:
            fixture["models"]["dcf"]["net_debt"]["source"]["as_of"] = (
                "2024-01-01T00:00:00Z"
            )

        result, output = self._run_with_driver(mutate_fixture=stale_source)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["quality"]["status"], "conditional")
        self.assertIn("key_source_stale:net_debt", driver["quality"]["flags"])

        def stale_scenario_source(driver: dict) -> None:
            for scenario in driver["scenarios"]:
                scenario["source"]["as_of"] = "2024-01-01T00:00:00Z"

        result, output = self._run_with_driver(mutate_driver=stale_scenario_source)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["quality"]["status"], "conditional")
        self.assertIn(
            "scenario_source_stale:conservative", driver["quality"]["flags"]
        )

    def test_driver_dcf_non_positive_terminal_fcf_is_unreliable(self) -> None:
        def mutate(driver: dict) -> None:
            # 终值年 capex 远超 NOPAT → 终值年 FCF 非正。
            driver["scenarios"][0]["capex"][-1] = 500.0

        result, output = self._run_with_driver(mutate_driver=mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(output.read_text(encoding="utf-8"))
        driver = artifact["results"]["driver_dcf"]
        self.assertEqual(driver["status"], "computed")
        self.assertEqual(driver["quality"]["status"], "unreliable")
        self.assertIn("non_positive_terminal_fcf", driver["quality"]["flags"])


if __name__ == "__main__":
    unittest.main()
