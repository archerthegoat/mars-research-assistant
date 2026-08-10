from __future__ import annotations

import json
import importlib.util
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from typing import Any, Callable


sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
QUALIFIED_FIXTURE = ROOT / "tests" / "fixtures" / "technical-analysis-demo.json"
YFINANCE_ENTRYPOINT = (
    ROOT
    / "skills"
    / "mars-research-assistant"
    / "skills"
    / "technical-analysis"
    / "scripts"
    / "analyze_with_yfinance.py"
)
DEMO_ARTIFACTS = ROOT / "examples" / "technical-analysis-demo"


class TechnicalAnalysisArtifactTests(unittest.TestCase):
    def _render(
        self,
        fixture: Path,
        output_dir: Path,
        *,
        no_open: bool = True,
        browser: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        environment["TMPDIR"] = str(output_dir.parent)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if browser is not None:
            environment["BROWSER"] = browser
        command = [
            sys.executable,
            "scripts/render_technical_analysis_fixture.py",
            "--input",
            str(fixture),
            "--output-dir",
            str(output_dir),
        ]
        if no_open:
            command.append("--no-open")
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def _write_fixture(
        self, directory: Path, update: Callable[[dict[str, object]], None]
    ) -> Path:
        fixture = json.loads(QUALIFIED_FIXTURE.read_text(encoding="utf-8"))
        update(fixture)
        fixture_path = directory / "fixture.json"
        fixture_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, allow_nan=True) + "\n",
            encoding="utf-8",
        )
        return fixture_path

    def _delivery(
        self, result: subprocess.CompletedProcess[str]
    ) -> dict[str, Any]:
        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, dict)
        return parsed

    def test_fixture_identity_is_carried_into_the_evidence_payload(self) -> None:
        identity = {
            "issuer_id": "issuer-demo",
            "listing_id": "DEMO.US",
            "case_id": "case-demo-001",
            "artifact_version": 1,
            "schema_version": 1,
        }
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def add_identity(fixture: dict[str, object]) -> None:
                fixture["identity"] = dict(identity)

            fixture_path = self._write_fixture(directory, add_identity)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(evidence["identity"], identity)
        self.assertEqual(evidence["schema_version"], 1)

    def test_invalid_fixture_identity_is_rejected_without_artifacts(self) -> None:
        cases = (
            ("missing-case-id", lambda identity: identity.pop("case_id")),
            (
                "non-one-schema-version",
                lambda identity: identity.update({"schema_version": 2}),
            ),
            (
                "non-one-artifact-version",
                lambda identity: identity.update({"artifact_version": 3}),
            ),
        )
        for name, mutate in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(
                prefix="technical-analysis-test-"
            ) as temporary:
                directory = Path(temporary)

                def add_identity(fixture: dict[str, object]) -> None:
                    identity = {
                        "issuer_id": "issuer-demo",
                        "listing_id": "DEMO.US",
                        "case_id": "case-demo-001",
                        "artifact_version": 1,
                        "schema_version": 1,
                    }
                    mutate(identity)
                    fixture["identity"] = identity

                fixture_path = self._write_fixture(directory, add_identity)
                output_dir = directory / "mars-research" / "artifacts"

                result = self._render(fixture_path, output_dir)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("identity", result.stdout + result.stderr)
                self.assertFalse(output_dir.exists())

    def test_provider_timestamp_after_research_as_of_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def move_provider_into_the_future(fixture: dict[str, object]) -> None:
                provider = fixture["provider"]
                assert isinstance(provider, dict)
                provider["as_of"] = "2027-01-01T00:00:00Z"

            fixture_path = self._write_fixture(directory, move_provider_into_the_future)
            output_dir = directory / "mars-research" / "artifacts"
            result = self._render(fixture_path, output_dir)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provider as_of must not be after research_as_of", result.stderr)
        self.assertFalse(output_dir.exists())

    def test_qualified_daily_history_creates_one_consistent_artifact_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            output_dir = Path(temporary) / "mars-research" / "artifacts"

            result = self._render(QUALIFIED_FIXTURE, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                sorted(path.name for path in output_dir.iterdir()),
                ["analysis.md", "evidence.json"],
            )
            delivery = json.loads(result.stdout)
            visualization = delivery["visualization"]
            self.assertTrue(visualization["generated"])
            self.assertFalse(visualization["open_attempted"])
            self.assertFalse(visualization["open_confirmed"])
            chart_path = Path(visualization["path"])
            self.assertNotEqual(chart_path.parent, output_dir)
            self.assertEqual(chart_path.name, "chart.html")
            self.assertTrue(chart_path.is_file())
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")
            html = chart_path.read_text(encoding="utf-8")

        evidence_id = evidence["evidence_id"]
        self.assertIn(evidence_id, markdown)
        self.assertIn(evidence_id, html)
        self.assertEqual(evidence["source"]["provider"], "yfinance")
        self.assertEqual(evidence["bars_used"], 319)
        self.assertEqual(len(evidence["ohlcv"]), 319)
        self.assertIn("LightweightCharts.createChart", html)
        self.assertIn("LightweightCharts.CandlestickSeries", html)
        self.assertIn("LightweightCharts.HistogramSeries", html)
        self.assertIn('id="chart-legend"', html)
        self.assertIn('id="crosshair-tooltip"', html)
        self.assertIn("subscribeCrosshairMove", html)
        self.assertIn("handleScroll", html)
        self.assertIn("handleScale", html)
        self.assertIn("smaLineStyles", html)

        self.assertIn("LightweightCharts.LineStyle.Dotted", html)
        self.assertIn("lastValueVisible: true", html)
        self.assertIn("@media (max-width: 640px)", html)
        self.assertIn("当前价", html)
        self.assertIn("支撑", html)
        self.assertIn("阻力", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("sessionStorage", html)
        self.assertNotIn("fetch(", html)
        self.assertIn(
            'href="https://www.tradingview.com/"',
            html,
        )
        payload_text = html.split(
            "const chartEvidence = ", maxsplit=1
        )[1].split(";\n", maxsplit=1)[0]
        payload = json.loads(payload_text)
        self.assertEqual(len(payload["candles"]), 120)
        self.assertEqual(len(payload["volume"]), 120)
        for window in (20, 50, 200):
            self.assertIn(f'"sma-{window}"', html)
        for provenance in (
            evidence["symbol"],
            evidence["source"]["label"],
            evidence["timezone"],
            evidence["as_of"],
            evidence["adjustment"],
            str(evidence["bars_used"]),
        ):
            self.assertIn(provenance, html)

    def test_output_directory_allows_a_custom_local_directory_and_rejects_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            output_dir = Path(temporary) / "custom-research" / "artifacts"
            result = self._render(QUALIFIED_FIXTURE, output_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((output_dir / "analysis.md").is_file())

        runtime_output = (
            ROOT
            / "skills"
            / "mars-research-assistant"
            / "skills"
            / "technical-analysis"
            / "blocked-technical-analysis"
        )
        try:
            result = self._render(QUALIFIED_FIXTURE, runtime_output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Skill runtime package", result.stderr)
            self.assertFalse(runtime_output.exists())
        finally:
            runtime_output.unlink(missing_ok=True)

    def test_short_history_fails_closed_with_an_exact_gap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def shorten(fixture: dict[str, object]) -> None:
                ohlcv = fixture["ohlcv"]
                assert isinstance(ohlcv, dict)
                bars = ohlcv["bars"]
                assert isinstance(bars, list)
                ohlcv["bars"] = bars[-318:]
                first = ohlcv["bars"][0]
                assert isinstance(first, dict)
                ohlcv["coverage_start"] = str(first["timestamp"])[:10]

            fixture_path = self._write_fixture(directory, shorten)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                [path.name for path in output_dir.iterdir()],
                ["analysis.md"],
            )
            delivery = self._delivery(result)
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")
            self.assertFalse(
                any(
                    path.name.startswith("mars-technical-chart-")
                    for path in directory.iterdir()
                )
            )

        self.assertEqual(delivery["artifacts"], ["analysis.md"])
        self.assertEqual(
            delivery["visualization"],
            {
                "generated": False,
                "kind": "temporary_html",
                "limitation": "visualization withheld because evidence did not qualify",
                "expires_after_seconds": None,
                "open_attempted": False,
                "open_confirmed": False,
                "path": None,
            },
        )
        self.assertIn("缺少 1 根已完成日线", markdown)
        self.assertIn("## 数据状态", markdown)
        self.assertIn("## 数据缺口", markdown)
        self.assertNotIn("## 技术结构", markdown)
        self.assertNotIn("## 关键位", markdown)
        self.assertNotIn("## 条件情景与失效", markdown)
        self.assertNotIn("<svg", markdown)

    def test_zero_close_bar_fails_closed_with_failure_package(self) -> None:
        # close=0（及同类零/负价格）必须转为 DataQualityError 失败包，
        # 而不是派生指标里的 ZeroDivisionError traceback。
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def zero_last_close(fixture: dict[str, object]) -> None:
                ohlcv = fixture["ohlcv"]
                assert isinstance(ohlcv, dict)
                bars = ohlcv["bars"]
                assert isinstance(bars, list)
                last = bars[-1]
                assert isinstance(last, dict)
                last["open"] = 0
                last["high"] = 0
                last["low"] = 0
                last["close"] = 0

            fixture_path = self._write_fixture(directory, zero_last_close)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertEqual(
                [path.name for path in output_dir.iterdir()],
                ["analysis.md"],
            )
            delivery = self._delivery(result)
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")

        self.assertEqual(delivery["artifacts"], ["analysis.md"])
        self.assertFalse(delivery["visualization"]["generated"])
        self.assertIn("positive prices", markdown)
        self.assertIn("## 数据缺口", markdown)

    def test_one_expanded_window_retry_can_recover_short_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def add_retry(fixture: dict[str, object]) -> None:
                qualified = fixture["ohlcv"]
                assert isinstance(qualified, dict)
                short = json.loads(json.dumps(qualified))
                short["bars"] = short["bars"][-318:]
                short["coverage_start"] = short["bars"][0]["timestamp"][:10]
                fixture["attempts"] = [short, qualified]
                fixture.pop("ohlcv")

            fixture_path = self._write_fixture(directory, add_retry)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(evidence["source"]["attempts"], 2)
        self.assertTrue(evidence["source"]["expanded_window_retry_used"])
        self.assertEqual(evidence["bars_used"], 319)

    def test_more_than_one_retry_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def add_three_attempts(fixture: dict[str, object]) -> None:
                qualified = fixture["ohlcv"]
                fixture["attempts"] = [qualified, qualified, qualified]

            fixture_path = self._write_fixture(directory, add_three_attempts)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "attempts must contain one or two OHLCV payloads",
                result.stdout + result.stderr,
            )
            self.assertFalse(output_dir.exists())

    def test_key_levels_are_computed_with_replayable_provenance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def inject_untrusted_levels(fixture: dict[str, object]) -> None:
                fixture["key_levels"] = [
                    {
                        "label": "不得采用",
                        "price": "1.23",
                        "condition": "这是旧提示词提供的数字",
                    }
                ]

            fixture_path = self._write_fixture(directory, inject_untrusted_levels)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")
            delivery = self._delivery(result)
            chart = Path(delivery["visualization"]["path"]).read_text(
                encoding="utf-8"
            )

        levels = evidence["key_levels"]
        self.assertLessEqual(
            len([level for level in levels if level["side"] == "support"]), 2
        )
        self.assertLessEqual(
            len([level for level in levels if level["side"] == "resistance"]), 2
        )
        self.assertNotIn(1.23, [level["price"] for level in levels])
        for level in levels:
            self.assertIn(
                level["method"],
                {"confirmed_swing_atr14_cluster", "120d_extreme_fallback"},
            )
            self.assertEqual(level["lookback"], 120)
            self.assertTrue(level["anchor_dates"])
            self.assertGreaterEqual(level["touches"], 1)
            self.assertIsInstance(level["price"], (int, float))
            self.assertIn(f"method={level['method']}", markdown)
            self.assertIn(f"lookback={level['lookback']}", markdown)
            self.assertIn(f"touches={level['touches']}", markdown)
            self.assertIn(f'"method":"{level["method"]}"', chart)
            self.assertIn(f'"touches":{level["touches"]}', chart)

    def test_summary_is_derived_from_normalized_evidence_metrics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            output_dir = Path(temporary) / "mars-research" / "artifacts"

            result = self._render(QUALIFIED_FIXTURE, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")

        metrics = evidence["derived_metrics"]
        self.assertEqual(
            metrics["price_vs_sma_pct"],
            {"20": 1.968929, "50": 3.794499, "200": 14.521603},
        )
        self.assertEqual(
            metrics["sma_direction"],
            {
                "20": {
                    "lookback_bars": 5,
                    "change_pct": 1.242299,
                    "direction": "rising",
                },
                "50": {
                    "lookback_bars": 5,
                    "change_pct": 0.608441,
                    "direction": "rising",
                },
                "200": {
                    "lookback_bars": 5,
                    "change_pct": 0.717134,
                    "direction": "rising",
                },
            },
        )
        self.assertEqual(
            metrics["completed_bar_returns_pct"],
            {"20": 5.379527, "60": 10.522486, "120": 17.803402},
        )
        self.assertEqual(metrics["atr14_pct_of_close"], 2.19691)
        self.assertEqual(metrics["volume_vs_20d_average_ratio"], 1.042714)
        self.assertEqual(metrics["drawdown_from_120d_high_pct"], 1.132665)
        self.assertEqual(metrics["distance_to_nearest_support_pct"], 5.9468)
        self.assertEqual(metrics["distance_to_nearest_resistance_pct"], 1.145641)
        self.assertEqual(
            evidence["priority_scenario"],
            {
                "name": "bull",
                "label": "多头",
                "basis": "technical_regime",
            },
        )

        conclusion = markdown.index("## 当前结论")
        explanation = markdown.index("## 趋势、位置与确认")
        self.assertLess(conclusion, explanation)
        self.assertIn("当前优先情景：**多头**", markdown)
        self.assertIn("较 SMA20 高 1.97%", markdown)
        self.assertIn("20/60/120 根收益分别为 5.38% / 10.52% / 17.8%", markdown)
        self.assertIn("ATR14 占收盘价 2.2%", markdown)
        self.assertIn("最新量为 20 日均量的 1.04 倍", markdown)
        self.assertIn("距 120 日高点回撤 1.13%", markdown)
        for scenario in ("多头情景", "震荡情景", "空头情景"):
            self.assertIn(f"### {scenario}", markdown)
        for dimension in ("支持条件", "有利表现", "不利表现", "触发条件", "失效条件"):
            self.assertEqual(markdown.count(f"- {dimension}："), 3)
        self.assertNotIn("概率", markdown)
        self.assertNotIn("置信度", markdown)

    def test_bear_and_range_summaries_never_invert_signed_evidence(self) -> None:
        def make_bear(fixture: dict[str, object]) -> None:
            ohlcv = fixture["ohlcv"]
            assert isinstance(ohlcv, dict)
            bars = ohlcv["bars"]
            assert isinstance(bars, list)
            for raw_bar in bars:
                assert isinstance(raw_bar, dict)
                old_open = float(raw_bar["open"])
                old_high = float(raw_bar["high"])
                old_low = float(raw_bar["low"])
                old_close = float(raw_bar["close"])
                raw_bar.update(
                    {
                        "open": 400 - old_open,
                        "high": 400 - old_low,
                        "low": 400 - old_high,
                        "close": 400 - old_close,
                    }
                )

        def make_range(fixture: dict[str, object]) -> None:
            ohlcv = fixture["ohlcv"]
            assert isinstance(ohlcv, dict)
            bars = ohlcv["bars"]
            assert isinstance(bars, list)
            for index, raw_bar in enumerate(bars):
                assert isinstance(raw_bar, dict)
                close = 200 + ((index % 10) - 5) / 10
                raw_bar.update(
                    {
                        "open": close - 0.05,
                        "high": close + 0.75,
                        "low": close - 0.75,
                        "close": close,
                    }
                )

        for expected_regime, mutate in (("空头", make_bear), ("震荡", make_range)):
            with self.subTest(regime=expected_regime), tempfile.TemporaryDirectory(
                prefix="technical-analysis-test-"
            ) as temporary:
                directory = Path(temporary)
                fixture_path = self._write_fixture(directory, mutate)
                output_dir = directory / "mars-research" / "artifacts"

                result = self._render(fixture_path, output_dir)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                evidence = json.loads(
                    (output_dir / "evidence.json").read_text(encoding="utf-8")
                )
                markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")

            self.assertEqual(evidence["regime"], expected_regime)
            self.assertIn(
                f"当前技术结构为**{expected_regime}**",
                markdown,
            )
            self.assertNotRegex(markdown, r"较 SMA(?:20|50|200) 高 -")
            if expected_regime == "空头":
                self.assertIn("较 SMA20 低", markdown)
                self.assertIn("当前满足空头支持条件", markdown)
                self.assertNotIn("当前趋势证据不支持空头优先", markdown)
            else:
                self.assertIn("均线次序分化", markdown)
                self.assertIn("当前满足震荡支持条件", markdown)

    def test_artifact_package_is_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)
            first = directory / "mars-research" / "first"
            second = directory / "mars-research" / "second"

            first_result = self._render(QUALIFIED_FIXTURE, first)
            second_result = self._render(QUALIFIED_FIXTURE, second)

            self.assertEqual(
                first_result.returncode, 0, first_result.stdout + first_result.stderr
            )
            self.assertEqual(
                second_result.returncode, 0, second_result.stdout + second_result.stderr
            )
            for artifact in ("analysis.md", "evidence.json"):
                self.assertEqual(
                    (first / artifact).read_bytes(),
                    (second / artifact).read_bytes(),
                    artifact,
                )
            first_delivery = self._delivery(first_result)
            second_delivery = self._delivery(second_result)
            self.assertNotEqual(
                first_delivery["visualization"]["path"],
                second_delivery["visualization"]["path"],
            )
            self.assertEqual(
                Path(first_delivery["visualization"]["path"]).read_bytes(),
                Path(second_delivery["visualization"]["path"]).read_bytes(),
            )

    def test_existing_output_directory_is_preserved_on_atomic_write_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            output_dir = Path(temporary) / "mars-research" / "artifacts"
            output_dir.mkdir(parents=True)
            sentinel = output_dir / "sentinel.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            result = self._render(QUALIFIED_FIXTURE, output_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("output_dir must not already exist", result.stdout + result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertEqual([path.name for path in output_dir.iterdir()], ["sentinel.txt"])
            self.assertFalse(
                any(
                    path.name.startswith("mars-technical-chart-")
                    for path in output_dir.parent.iterdir()
                )
            )

    def test_default_delivery_attempts_browser_open_and_reports_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            output_dir = Path(temporary) / "mars-research" / "artifacts"

            result = self._render(
                QUALIFIED_FIXTURE,
                output_dir,
                no_open=False,
                browser="/bin/false %s",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            delivery = self._delivery(result)

        visualization = delivery["visualization"]
        self.assertTrue(visualization["generated"])
        self.assertTrue(visualization["open_attempted"])
        self.assertFalse(visualization["open_confirmed"])
        self.assertIn("did not confirm", visualization["limitation"])

    def test_vendored_lightweight_charts_has_fixed_version_and_license(
        self,
    ) -> None:
        vendor = (
            ROOT
            / "skills"
            / "mars-research-assistant"
            / "skills"
            / "technical-analysis"
            / "vendor"
            / "lightweight-charts"
            / "5.2.0"
        )

        script = vendor / "lightweight-charts.standalone.production.js"
        self.assertTrue(script.is_file())
        self.assertIn(
            "Apache License",
            (vendor / "LICENSE").read_text(encoding="utf-8"),
        )
        notice = (vendor / "NOTICE.md").read_text(encoding="utf-8")
        self.assertIn("lightweight-charts@5.2.0", notice)
        self.assertIn(
            "c0992580867c4912cc9385b3c2728315bcc1a76c7f1087dca908430fccdf31d7",
            notice,
        )
        self.assertEqual(
            sha256(script.read_bytes()).hexdigest(),
            "c0992580867c4912cc9385b3c2728315bcc1a76c7f1087dca908430fccdf31d7",
        )

    def test_next_run_cleans_expired_charts_without_removing_fresh_ones(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)
            first_output = directory / "mars-research" / "first"
            first_result = self._render(QUALIFIED_FIXTURE, first_output)
            self.assertEqual(
                first_result.returncode,
                0,
                first_result.stdout + first_result.stderr,
            )
            first_delivery = self._delivery(first_result)
            expired = Path(first_delivery["visualization"]["path"]).parent
            os.utime(expired, (0, 0))
            unowned = directory / "mars-research" / "mars-technical-chart-unowned"
            unowned.mkdir()
            (unowned / "chart.html").write_text("not ours", encoding="utf-8")
            os.utime(unowned, (0, 0))

            second_output = directory / "mars-research" / "second"
            second_result = self._render(QUALIFIED_FIXTURE, second_output)
            self.assertEqual(
                second_result.returncode,
                0,
                second_result.stdout + second_result.stderr,
            )
            second_delivery = self._delivery(second_result)

            self.assertFalse(expired.exists())
            self.assertTrue(unowned.is_dir())
            self.assertTrue(
                Path(second_delivery["visualization"]["path"]).is_file()
            )

    def test_market_context_never_changes_technical_evidence_or_chart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)
            without_context = directory / "mars-research" / "without-context"
            with_context = directory / "mars-research" / "with-context"

            def add_context(fixture: dict[str, object]) -> None:
                fixture["market_context"] = {
                    "status": "available",
                    "as_of": "2026-06-22T08:30:00-04:00",
                    "source": "市场快照工件",
                    "timezone": "Etc/GMT+4",
                    "regime": "risk_on",
                    "summary": "风险偏好改善，但行业广度仍有分化。",
                    "same_run": True,
                }

            contextual_fixture = self._write_fixture(directory, add_context)
            plain_result = self._render(QUALIFIED_FIXTURE, without_context)
            context_result = self._render(contextual_fixture, with_context)

            self.assertEqual(
                plain_result.returncode, 0, plain_result.stdout + plain_result.stderr
            )
            self.assertEqual(
                context_result.returncode,
                0,
                context_result.stdout + context_result.stderr,
            )
            plain_evidence = json.loads(
                (without_context / "evidence.json").read_text(encoding="utf-8")
            )
            context_evidence = json.loads(
                (with_context / "evidence.json").read_text(encoding="utf-8")
            )
            context_markdown = (with_context / "analysis.md").read_text(
                encoding="utf-8"
            )
            plain_delivery = self._delivery(plain_result)
            context_delivery = self._delivery(context_result)
            plain_chart = Path(
                plain_delivery["visualization"]["path"]
            ).read_bytes()
            context_chart = Path(
                context_delivery["visualization"]["path"]
            ).read_bytes()

        self.assertEqual(
            plain_evidence["evidence_id"], context_evidence["evidence_id"]
        )
        self.assertEqual(
            plain_evidence["indicators"], context_evidence["indicators"]
        )
        self.assertEqual(
            plain_evidence["key_levels"], context_evidence["key_levels"]
        )
        self.assertEqual(plain_chart, context_chart)
        self.assertIn("风险偏好改善，但行业广度仍有分化", context_markdown)
        self.assertIn("与当前多头优先情景形成共振", context_markdown)
        self.assertIn("不改变图表、指标或关键位", context_markdown)

    def test_stale_market_context_degrades_to_technical_only_analysis(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def add_stale_context(fixture: dict[str, object]) -> None:
                fixture["market_context"] = {
                    "status": "available",
                    "as_of": "2026-06-20T08:00:00-04:00",
                    "source": "市场快照工件",
                    "timezone": "Etc/GMT+4",
                    "regime": "neutral",
                    "summary": "该摘要已经过期。",
                    "same_run": False,
                }

            fixture_path = self._write_fixture(directory, add_stale_context)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")

        self.assertEqual(evidence["market_context"]["status"], "stale")
        self.assertIn("超过 24 小时有效期", markdown)
        self.assertIn("仅基于技术面证据", markdown)
        self.assertNotIn("该摘要已经过期", markdown)

    def test_each_blocking_ohlcv_quality_gate_fails_closed(self) -> None:
        def missing_volume(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["bars"][-1].pop("volume")

        def non_finite_close(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["bars"][-1]["close"] = float("nan")

        def nonpositive_volume(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["bars"][-1]["volume"] = 0

        def timezone_less(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["bars"][-1]["timestamp"] = "2026-06-19T16:00:00"

        def missing_timezone(fixture: dict[str, object]) -> None:
            fixture["ohlcv"].pop("timezone")

        def invalid_timezone(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["timezone"] = "Mars/Olympus_Mons"

        def mismatched_timezone_offset(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["bars"][-1]["timestamp"] = (
                "2026-06-19T16:00:00+08:00"
            )

        def unadjusted(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["adjustment"] = "unadjusted"

        def out_of_order(fixture: dict[str, object]) -> None:
            bars = fixture["ohlcv"]["bars"]
            bars[-2], bars[-1] = bars[-1], bars[-2]

        def invalid_bounds(fixture: dict[str, object]) -> None:
            bar = fixture["ohlcv"]["bars"][-1]
            bar["low"] = bar["high"] + 1

        def uncovered_range(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["coverage_start"] = "2020-01-01"

        def reversed_range(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["coverage_start"] = "2026-06-19"
            fixture["ohlcv"]["coverage_end"] = "2025-04-01"

        def incomplete_middle_bar(fixture: dict[str, object]) -> None:
            fixture["ohlcv"]["bars"][-2]["complete"] = False

        cases = (
            (missing_volume, "numeric volume"),
            (non_finite_close, "finite close"),
            (nonpositive_volume, "positive volume"),
            (timezone_less, "timezone-aware timestamp"),
            (missing_timezone, "OHLCV requires text"),
            (invalid_timezone, "valid IANA timezone"),
            (mismatched_timezone_offset, "does not match declared timezone"),
            (unadjusted, "adjustment must be adjusted"),
            (out_of_order, "strictly increasing"),
            (invalid_bounds, "inconsistent price bounds"),
            (uncovered_range, "must exactly match actual bars"),
            (reversed_range, "coverage start must not follow coverage end"),
            (incomplete_middle_bar, "only the latest OHLCV bar may be incomplete"),
        )
        for mutate, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory(
                prefix="technical-analysis-test-"
            ) as temporary:
                directory = Path(temporary)
                fixture_path = self._write_fixture(directory, mutate)
                output_dir = directory / "mars-research" / "artifacts"

                result = self._render(fixture_path, output_dir)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(
                    [path.name for path in output_dir.iterdir()],
                    ["analysis.md"],
                )
                markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")
                self.assertIn(reason, markdown)
                self.assertNotIn("## 技术结构", markdown)
                self.assertNotIn("## 关键位", markdown)
                self.assertNotIn("## 条件情景与失效", markdown)

    def test_latest_incomplete_bar_is_safely_removed_before_analysis(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def add_incomplete_latest_bar(fixture: dict[str, object]) -> None:
                bars = fixture["ohlcv"]["bars"]
                extra = dict(bars[-1])
                extra["timestamp"] = "2026-06-20T16:00:00-04:00"
                extra["complete"] = False
                bars.append(extra)
                fixture["ohlcv"]["coverage_end"] = "2026-06-19"

            fixture_path = self._write_fixture(directory, add_incomplete_latest_bar)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")

        self.assertEqual(evidence["bars_used"], 319)
        self.assertTrue(evidence["stripped_incomplete_latest_bar"])
        self.assertIn("已安全剔除一根未完成的最新日线", markdown)

    def test_non_yfinance_provider_is_rejected_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def change_provider(fixture: dict[str, object]) -> None:
                fixture["provider"] = {
                    "name": "custom",
                    "kind": "custom",
                    "status": "available",
                    "as_of": "2026-06-19T16:00:00-04:00",
                }

            fixture_path = self._write_fixture(directory, change_provider)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("provider must be yfinance EOD", result.stdout + result.stderr)
            self.assertFalse(output_dir.exists())

    def test_zero_atr_fails_closed_instead_of_aborting_without_a_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)

            def flatten_prices(fixture: dict[str, object]) -> None:
                for bar in fixture["ohlcv"]["bars"]:
                    bar.update(
                        {
                            "open": 100,
                            "high": 100,
                            "low": 100,
                            "close": 100,
                        }
                    )

            fixture_path = self._write_fixture(directory, flatten_prices)
            output_dir = directory / "mars-research" / "artifacts"

            result = self._render(fixture_path, output_dir)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                [path.name for path in output_dir.iterdir()],
                ["analysis.md"],
            )
            markdown = (output_dir / "analysis.md").read_text(encoding="utf-8")

        self.assertIn("ATR14 requires positive price ranges", markdown)
        self.assertNotIn("## 技术结构", markdown)

    def test_yfinance_adapter_retries_once_with_a_larger_window(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "analyze_with_yfinance", YFINANCE_ENTRYPOINT
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = json.loads(QUALIFIED_FIXTURE.read_text(encoding="utf-8"))
        bars = source["ohlcv"]["bars"]

        class FakeFrame:
            timezone = "Etc/GMT+4"

            def __init__(self, fixture_bars: list[dict[str, object]]) -> None:
                self._bars = fixture_bars
                self.empty = not fixture_bars

            def iterrows(self):
                for bar in self._bars:
                    timestamp = datetime.fromisoformat(str(bar["timestamp"]))
                    yield timestamp, {
                        "Open": bar["open"],
                        "High": bar["high"],
                        "Low": bar["low"],
                        "Close": bar["close"],
                        "Volume": bar["volume"],
                    }

        class FakeTicker:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def history(self, **kwargs):
                self.calls.append(kwargs)
                selected = bars[-318:] if len(self.calls) == 1 else bars
                return FakeFrame(selected)

        ticker = FakeTicker()
        now = datetime.fromisoformat("2026-06-22T09:00:00-04:00")

        fixture = module.build_yfinance_fixture(
            "DEMO",
            ticker_factory=lambda symbol: ticker,
            now=now,
        )

        self.assertEqual(
            [call["period"] for call in ticker.calls],
            ["18mo", "3y"],
        )
        self.assertTrue(all(call["auto_adjust"] is True for call in ticker.calls))
        self.assertTrue(all(call["repair"] is False for call in ticker.calls))
        self.assertTrue(all(call["end"] == "2026-06-22" for call in ticker.calls))
        self.assertEqual(fixture["provider"]["name"], "yfinance EOD")
        self.assertEqual(fixture["provider"]["kind"], "public_best_effort")
        self.assertEqual(len(fixture["attempts"]), 2)
        self.assertEqual(len(fixture["attempts"][0]["bars"]), 318)
        self.assertEqual(len(fixture["attempts"][1]["bars"]), 319)
        self.assertNotIn("api_key", json.dumps(fixture).lower())

    def test_yfinance_network_failure_is_counted_before_a_successful_retry(
        self,
    ) -> None:
        spec = importlib.util.spec_from_file_location(
            "analyze_with_yfinance_retry", YFINANCE_ENTRYPOINT
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = json.loads(QUALIFIED_FIXTURE.read_text(encoding="utf-8"))
        bars = source["ohlcv"]["bars"]

        class FakeFrame:
            empty = False
            timezone = "Etc/GMT+4"

            def iterrows(self):
                for bar in bars:
                    yield datetime.fromisoformat(bar["timestamp"]), {
                        "Open": bar["open"],
                        "High": bar["high"],
                        "Low": bar["low"],
                        "Close": bar["close"],
                        "Volume": bar["volume"],
                    }

        class FlakyTicker:
            def __init__(self) -> None:
                self.calls = 0

            def history(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("first request timed out")
                return FakeFrame()

        ticker = FlakyTicker()
        fixture = module.build_yfinance_fixture(
            "DEMO",
            ticker_factory=lambda symbol: ticker,
            now=datetime.fromisoformat("2026-06-22T09:00:00-04:00"),
        )

        self.assertEqual(ticker.calls, 2)
        self.assertEqual(fixture["source_attempts"], 2)
        self.assertTrue(fixture["expanded_window_retry_used"])
        self.assertEqual(len(fixture["attempts"]), 1)

        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            directory = Path(temporary)
            fixture_path = directory / "fixture.json"
            fixture_path.write_text(
                json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output_dir = directory / "mars-research" / "artifacts"
            result = self._render(fixture_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(
                (output_dir / "evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(evidence["source"]["attempts"], 2)
        self.assertTrue(evidence["source"]["expanded_window_retry_used"])

    def test_same_day_bar_is_not_guessed_incomplete_after_download(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "analyze_with_yfinance_same_day", YFINANCE_ENTRYPOINT
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = json.loads(QUALIFIED_FIXTURE.read_text(encoding="utf-8"))
        bars = source["ohlcv"]["bars"]
        same_day = dict(bars[-1])
        same_day["timestamp"] = "2026-06-22T16:00:00-04:00"

        class FakeFrame:
            empty = False
            timezone = "Etc/GMT+4"

            def iterrows(self):
                for bar in [*bars[:-1], same_day]:
                    yield datetime.fromisoformat(bar["timestamp"]), {
                        "Open": bar["open"],
                        "High": bar["high"],
                        "Low": bar["low"],
                        "Close": bar["close"],
                        "Volume": bar["volume"],
                    }

        class FakeTicker:
            def history(self, **kwargs):
                return FakeFrame()

        fixture = module.build_yfinance_fixture(
            "DEMO",
            ticker_factory=lambda symbol: FakeTicker(),
            now=datetime.fromisoformat("2026-06-22T18:00:00-04:00"),
        )

        self.assertNotIn("complete", fixture["attempts"][0]["bars"][-1])

    def test_unreadable_market_context_degrades_without_blocking(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "analyze_with_yfinance_context", YFINANCE_ENTRYPOINT
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            missing = Path(temporary) / "missing.json"
            invalid = Path(temporary) / "invalid.json"
            invalid.write_text("{not-json", encoding="utf-8")

            missing_context = module.load_market_context(missing)
            invalid_context = module.load_market_context(invalid)

        self.assertEqual(missing_context["status"], "unavailable")
        self.assertIn("FileNotFoundError", missing_context["reason"])
        self.assertEqual(invalid_context["status"], "invalid")
        self.assertIn("JSONDecodeError", invalid_context["reason"])

    def test_readme_demo_is_generated_from_the_offline_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="technical-analysis-test-") as temporary:
            regenerated = Path(temporary) / "mars-research" / "regenerated"

            result = self._render(QUALIFIED_FIXTURE, regenerated)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for artifact in ("analysis.md", "evidence.json"):
                self.assertEqual(
                    (regenerated / artifact).read_bytes(),
                    (DEMO_ARTIFACTS / artifact).read_bytes(),
                    artifact,
                )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("assets/technical-analysis-showcase.png", readme)
        self.assertIn("临时 Lightweight Charts HTML", readme)
        self.assertNotIn("chart.svg", readme)
        self.assertNotIn("# Mars Skills 1.", readme)


if __name__ == "__main__":
    unittest.main()
