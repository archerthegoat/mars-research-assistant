from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from typing import Any


sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "mars-research-assistant"
    / "skills"
    / "deep-equity-research"
    / "scripts"
    / "earnings_quality.py"
)
RULES_PATH = SCRIPT.parents[1] / "reference" / "earnings_quality_rules.json"
FIXTURES = ROOT / "tests" / "fixtures"
SHORT_ADVICE = re.compile(r"做空|沽空|卖空|\bshort\b", re.IGNORECASE)

BENEISH_COEFFICIENTS = {
    "DSRI": 0.92,
    "GMI": 0.528,
    "AQI": 0.404,
    "SGI": 0.892,
    "DEPI": 0.115,
    "SGAI": -0.172,
    "LVGI": -0.327,
    "TATA": 4.679,
}


def _independent_m_score(fixture: dict[str, Any]) -> float:
    beneish = fixture["components"]["beneish"]
    cur = beneish["current"]
    pri = beneish["prior"]
    variables = {
        "DSRI": (cur["receivables"] / cur["revenue"])
        / (pri["receivables"] / pri["revenue"]),
        "GMI": ((pri["revenue"] - pri["cogs"]) / pri["revenue"])
        / ((cur["revenue"] - cur["cogs"]) / cur["revenue"]),
        "AQI": (1 - (cur["current_assets"] + cur["net_ppe"]) / cur["total_assets"])
        / (1 - (pri["current_assets"] + pri["net_ppe"]) / pri["total_assets"]),
        "SGI": cur["revenue"] / pri["revenue"],
        "DEPI": (pri["depreciation"] / (pri["depreciation"] + pri["net_ppe"]))
        / (cur["depreciation"] / (cur["depreciation"] + cur["net_ppe"])),
        "SGAI": (cur["sga_expense"] / cur["revenue"])
        / (pri["sga_expense"] / pri["revenue"]),
        "LVGI": (cur["total_liabilities"] / cur["total_assets"])
        / (pri["total_liabilities"] / pri["total_assets"]),
        "TATA": (cur["net_income"] - cur["operating_cash_flow"])
        / cur["total_assets"],
    }
    return -4.84 + sum(
        BENEISH_COEFFICIENTS[name] * value for name, value in variables.items()
    )


class EarningsQualityTests(unittest.TestCase):
    def _run(
        self, fixture_path: Path, output_path: Path
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--input",
                str(fixture_path),
                "--output",
                str(output_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def _report(
        self, fixture_name: str, directory: Path
    ) -> tuple[dict[str, Any], str]:
        result, output = self._run_raw(fixture_name, directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = output.read_text(encoding="utf-8")
        return json.loads(text), text

    def _run_raw(
        self, fixture_name: str, directory: Path, output_name: str = "earnings-quality.json"
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        output = directory / output_name
        result = self._run(FIXTURES / fixture_name, output)
        return result, output

    def _report_from_payload(
        self, payload: dict[str, Any], directory: Path
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        fixture_path = directory / "inputs.json"
        fixture_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        output = directory / "earnings-quality.json"
        return self._run(fixture_path, output), output

    def _base_payload(self) -> dict[str, Any]:
        return json.loads(
            (FIXTURES / "earnings-quality-a.json").read_text(encoding="utf-8")
        )

    def test_sufficient_baseline_is_recorded_without_provisional(self) -> None:
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            report, _ = self._report("earnings-quality-a.json", Path(temporary))
        periods = report["accounting_periods"]
        self.assertEqual(periods["status"], "recorded")
        self.assertEqual(periods["annual_count"], 3)
        self.assertEqual(periods["annual"], ["FY2023", "FY2024", "FY2025"])
        self.assertEqual(periods["quarter_count"], 8)
        self.assertEqual(len(periods["quarters"]), 8)
        self.assertFalse(report["provisional"])
        self.assertEqual(report["data_gaps"], [])

    def test_short_annual_baseline_marks_provisional_with_gap(self) -> None:
        fixture = self._base_payload()
        fixture["accounting_periods"]["annual"] = ["FY2024", "FY2025"]
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            result, output = self._report_from_payload(fixture, Path(temporary))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["provisional"])
        self.assertIn("基线", report["provisional_reason"])
        self.assertEqual(report["accounting_periods"]["annual_count"], 2)
        self.assertTrue(
            any("年度基线仅 2 年" in gap for gap in report["data_gaps"]),
            report["data_gaps"],
        )

    def test_annual_and_annual_years_both_present_fails_closed(self) -> None:
        fixture = self._base_payload()
        fixture["accounting_periods"]["annual_years"] = ["FY2023", "FY2024", "FY2025"]
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            result, output = self._report_from_payload(fixture, Path(temporary))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "accounting_periods must not provide both annual_years and annual",
                result.stderr,
            )
            self.assertFalse(output.exists())

    def test_annual_years_contract_key_is_accepted(self) -> None:
        fixture = self._base_payload()
        annual = fixture["accounting_periods"].pop("annual")
        fixture["accounting_periods"]["annual_years"] = annual
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            result, output = self._report_from_payload(fixture, Path(temporary))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        periods = report["accounting_periods"]
        self.assertEqual(periods["status"], "recorded")
        self.assertEqual(periods["annual_count"], 3)
        self.assertEqual(periods["annual"], ["FY2023", "FY2024", "FY2025"])
        self.assertFalse(report["provisional"])
        self.assertEqual(report["data_gaps"], [])

    def test_short_annual_years_baseline_reflects_real_count(self) -> None:
        fixture = self._base_payload()
        fixture["accounting_periods"].pop("annual")
        fixture["accounting_periods"]["annual_years"] = ["FY2024", "FY2025"]
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            result, output = self._report_from_payload(fixture, Path(temporary))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["provisional"])
        self.assertIn("基线", report["provisional_reason"])
        self.assertEqual(report["accounting_periods"]["annual_count"], 2)
        self.assertTrue(
            any("年度基线仅 2 年" in gap for gap in report["data_gaps"]),
            report["data_gaps"],
        )

    def test_short_quarter_baseline_marks_provisional_with_gap(self) -> None:
        fixture = self._base_payload()
        fixture["accounting_periods"]["quarters"] = [
            "2025Q1",
            "2025Q2",
            "2025Q3",
            "2025Q4",
        ]
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            result, output = self._report_from_payload(fixture, Path(temporary))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["provisional"])
        self.assertIn("基线", report["provisional_reason"])
        self.assertEqual(report["accounting_periods"]["quarter_count"], 4)
        self.assertTrue(
            any("季度基线仅 4 个" in gap for gap in report["data_gaps"]),
            report["data_gaps"],
        )

    def test_missing_accounting_periods_marks_provisional_with_gap(self) -> None:
        fixture = self._base_payload()
        fixture.pop("accounting_periods")
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            result, output = self._report_from_payload(fixture, Path(temporary))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["provisional"])
        self.assertIn("基线", report["provisional_reason"])
        self.assertEqual(report["accounting_periods"]["status"], "missing")
        self.assertTrue(
            any("accounting_periods" in gap for gap in report["data_gaps"]),
            report["data_gaps"],
        )

    def test_invalid_accounting_periods_fails_closed(self) -> None:
        for broken in (
            ["FY2023"],
            {"annual": "FY2023", "quarters": []},
            {"annual": ["FY2023", 42, "FY2025"], "quarters": []},
        ):
            fixture = self._base_payload()
            fixture["accounting_periods"] = broken
            with self.subTest(broken=broken), tempfile.TemporaryDirectory(
                prefix="earnings-quality-test-"
            ) as temporary:
                result, output = self._report_from_payload(fixture, Path(temporary))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("accounting_periods", result.stderr)
                self.assertFalse(output.exists())

    def test_clean_company_grades_a_without_veto(self) -> None:
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            report, _ = self._report("earnings-quality-a.json", Path(temporary))
        self.assertEqual(report["grade"], "A")
        self.assertFalse(report["provisional"])
        self.assertIsNone(report["provisional_reason"])
        self.assertFalse(report["long_entry_veto"])
        self.assertIsNone(report["veto_reason"])
        for key in (
            "accruals",
            "beneish",
            "revenue_recognition",
            "cash_flow",
            "audit_governance",
        ):
            self.assertEqual(report["components"][key]["status"], "computed", key)
        self.assertEqual(report["components"]["beneish"]["threshold"], -1.78)
        self.assertFalse(report["components"]["beneish"]["manipulation_likely"])
        self.assertEqual(report["data_gaps"], [])

    def test_high_accruals_and_red_flags_grade_c_with_veto(self) -> None:
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            report, _ = self._report("earnings-quality-c.json", Path(temporary))
        self.assertEqual(report["grade"], "C")
        self.assertTrue(report["long_entry_veto"])
        self.assertIn("long_entry_veto", report["veto_reason"])
        self.assertFalse(report["provisional"])
        flags = {
            flag["id"]: flag
            for flag in report["components"]["revenue_recognition"]["red_flags"]
        }
        self.assertTrue(flags["receivables_growth_exceeds_revenue_growth"]["triggered"])
        self.assertTrue(flags["related_party_revenue_share"]["triggered"])
        self.assertFalse(flags["deferred_revenue_decline"]["triggered"])
        self.assertFalse(flags["quarter_end_revenue_spike"]["triggered"])
        self.assertLess(report["components"]["accruals"]["score"], 100)
        self.assertLess(report["components"]["cash_flow"]["score"], 50)

    def test_beneish_not_applicable_marks_grade_provisional(self) -> None:
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            report, _ = self._report(
                "earnings-quality-beneish-na.json", Path(temporary)
            )
        beneish = report["components"]["beneish"]
        self.assertEqual(beneish["status"], "not_applicable")
        self.assertIn("cogs", beneish["reason"])
        self.assertTrue(report["provisional"])
        self.assertIn("暂定级", report["provisional_reason"])
        self.assertIn("beneish(not_applicable)", report["provisional_reason"])
        self.assertEqual(report["grade"], "A")
        self.assertFalse(report["long_entry_veto"])
        self.assertTrue(
            any("beneish" in gap for gap in report["data_gaps"]),
            report["data_gaps"],
        )

    def test_beneish_missing_when_section_absent(self) -> None:
        fixture = json.loads(
            (FIXTURES / "earnings-quality-a.json").read_text(encoding="utf-8")
        )
        fixture["components"].pop("beneish")
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            directory = Path(temporary)
            fixture_path = directory / "inputs.json"
            fixture_path.write_text(
                json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output = directory / "earnings-quality.json"
            result = self._run(fixture_path, output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["components"]["beneish"]["status"], "missing")
        self.assertTrue(report["provisional"])
        self.assertIn("beneish(missing)", report["provisional_reason"])

    def test_beneish_zero_denominator_degrades_to_not_applicable(self) -> None:
        cases = {
            "prior sga_expense": lambda beneish: beneish["prior"].update(sga_expense=0),
            "prior asset-quality term": lambda beneish: beneish["prior"].update(
                current_assets=(
                    beneish["prior"]["total_assets"] - beneish["prior"]["net_ppe"]
                )
            ),
            "prior total_liabilities": lambda beneish: beneish["prior"].update(
                total_liabilities=0
            ),
            "prior receivables": lambda beneish: beneish["prior"].update(receivables=0),
            "current depreciation": lambda beneish: beneish["current"].update(
                depreciation=0
            ),
            "current gross profit": lambda beneish: beneish["current"].update(
                cogs=beneish["current"]["revenue"]
            ),
            "prior revenue": lambda beneish: beneish["prior"].update(revenue=0),
            "current total_assets": lambda beneish: beneish["current"].update(
                total_assets=0
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory(
                prefix="earnings-quality-test-"
            ) as temporary:
                fixture = self._base_payload()
                mutate(fixture["components"]["beneish"])
                result, output = self._report_from_payload(fixture, Path(temporary))
                self.assertEqual(
                    result.returncode, 0, f"{name}: {result.stdout}{result.stderr}"
                )
                self.assertNotIn("Traceback", result.stderr)
                report = json.loads(output.read_text(encoding="utf-8"))
            beneish = report["components"]["beneish"]
            self.assertEqual(beneish["status"], "not_applicable", name)
            self.assertIn("分母为零", beneish["reason"], name)
            self.assertTrue(report["provisional"], name)
            self.assertIn("beneish(not_applicable)", report["provisional_reason"], name)
            self.assertTrue(
                any(
                    "beneish" in gap and "分母为零" in gap
                    for gap in report["data_gaps"]
                ),
                f"{name}: {report['data_gaps']}",
            )

    def test_non_standard_audit_opinion_grades_d_with_veto(self) -> None:
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            report, _ = self._report("earnings-quality-d.json", Path(temporary))
        self.assertEqual(report["grade"], "D")
        self.assertTrue(report["long_entry_veto"])
        self.assertFalse(report["provisional"])
        signals = report["components"]["audit_governance"]["signals"]
        opinion = next(s for s in signals if s["signal"] == "audit_opinion")
        self.assertEqual(opinion["detail"], "adverse")
        self.assertEqual(opinion["severity"], "critical")
        self.assertTrue(report["components"]["beneish"]["manipulation_likely"])
        triggered = [
            flag["id"]
            for flag in report["components"]["revenue_recognition"]["red_flags"]
            if flag["triggered"]
        ]
        self.assertEqual(len(triggered), 4)

    def test_m_score_matches_independent_recomputation(self) -> None:
        for fixture_name in ("earnings-quality-a.json", "earnings-quality-c.json"):
            with self.subTest(fixture=fixture_name), tempfile.TemporaryDirectory(
                prefix="earnings-quality-test-"
            ) as temporary:
                report, _ = self._report(fixture_name, Path(temporary))
                fixture = json.loads(
                    (FIXTURES / fixture_name).read_text(encoding="utf-8")
                )
                expected = _independent_m_score(fixture)
                self.assertAlmostEqual(
                    report["components"]["beneish"]["m_score"], expected, places=5
                )
                variables = report["components"]["beneish"]["variables"]
                self.assertEqual(
                    sorted(variables),
                    ["AQI", "DEPI", "DSRI", "GMI", "LVGI", "SGAI", "SGI", "TATA"],
                )

    def test_rules_version_is_written_to_output(self) -> None:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(rules["rules_version"], "v1.0.3-eq-1")
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            report, _ = self._report("earnings-quality-a.json", Path(temporary))
        self.assertEqual(report["rules_version"], rules["rules_version"])
        self.assertEqual(
            report["engine"],
            "skills/deep-equity-research/scripts/earnings_quality.py",
        )

    def test_output_carries_top_level_schema_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            report, _ = self._report("earnings-quality-a.json", Path(temporary))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["identity"]["artifact_version"], 1)
        self.assertEqual(report["identity"]["schema_version"], 1)

    def test_missing_or_non_one_schema_version_fails_closed(self) -> None:
        for name, mutate in (
            ("missing", lambda fixture: fixture.pop("schema_version")),
            ("non-one", lambda fixture: fixture.update({"schema_version": 2})),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory(
                prefix="earnings-quality-test-"
            ) as temporary:
                fixture = self._base_payload()
                mutate(fixture)
                result, output = self._report_from_payload(fixture, Path(temporary))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema_version must be 1", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertFalse(output.exists())

    def test_identity_versions_must_be_one(self) -> None:
        for field in ("artifact_version", "schema_version"):
            with self.subTest(field=field), tempfile.TemporaryDirectory(
                prefix="earnings-quality-test-"
            ) as temporary:
                fixture = self._base_payload()
                fixture["identity"][field] = 2
                result, output = self._report_from_payload(fixture, Path(temporary))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"identity {field} must be 1", result.stderr)
                self.assertFalse(output.exists())

    def test_existing_output_path_fails_nonzero_and_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory(prefix="earnings-quality-test-") as temporary:
            directory = Path(temporary)
            first, output = self._run_raw("earnings-quality-a.json", directory)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            original = output.read_text(encoding="utf-8")
            second, _ = self._run_raw("earnings-quality-a.json", directory)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("File exists", second.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_output_inside_runtime_package_is_rejected(self) -> None:
        runtime_output = (
            ROOT
            / "skills"
            / "mars-research-assistant"
            / "blocked-earnings-quality.json"
        )
        try:
            result = self._run(FIXTURES / "earnings-quality-a.json", runtime_output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Skill runtime package", result.stderr)
            self.assertFalse(runtime_output.exists())
        finally:
            runtime_output.unlink(missing_ok=True)

    def test_output_never_contains_short_selling_advice(self) -> None:
        for fixture_name in (
            "earnings-quality-a.json",
            "earnings-quality-c.json",
            "earnings-quality-beneish-na.json",
            "earnings-quality-d.json",
        ):
            with self.subTest(fixture=fixture_name), tempfile.TemporaryDirectory(
                prefix="earnings-quality-test-"
            ) as temporary:
                _, text = self._report(fixture_name, Path(temporary))
                self.assertIsNone(SHORT_ADVICE.search(text), fixture_name)


if __name__ == "__main__":
    unittest.main()
