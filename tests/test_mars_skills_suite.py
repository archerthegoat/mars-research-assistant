from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "skills" / "mars-research-assistant"
SNAPSHOT_RENDERER = RUNTIME / "scripts" / "render_equity_snapshot.py"
UNDERWRITING_RENDERER = (
    RUNTIME / "skills" / "deep-equity-research" / "scripts" / "render_underwriting.py"
)
SNAPSHOT_FIXTURE = ROOT / "tests" / "fixtures" / "equity-snapshot-primary.json"
UNDERWRITING_FIXTURE = ROOT / "tests" / "fixtures" / "underwriting-inputs-initial.json"
EXPECTED_SKILLS = {
    "ask-mars",
    "market-catalysts-brief",
    "market-snapshot",
    "instrument-research",
    "deep-equity-research",
    "technical-analysis",
    "investment-analysis",
    "drive-writeback",
}
UNDERWRITING_CHAPTERS = (
    "研究范围、预注册命题与交易结论",
    "公司、业务模式与价值驱动",
    "行业结构、竞争与行业专属反证",
    "管理层、治理与资本配置",
    "财务、分部/KPI 与财报质量",
    "预期差、催化剂、基准率与跟踪清单",
    "可复算估值与“现价定价了什么”",
    "反方论证、事前风险预演与可证伪条件",
    "来源、数据对账、时间戳、假设与数据缺口",
)
DEVELOPMENT_DIR_PARTS = {"tests", "docs", ".git", ".venv", "__pycache__"}


def runtime_files() -> list[Path]:
    return sorted(
        path
        for path in RUNTIME.rglob("*")
        if path.is_file()
        and not any(
            part in DEVELOPMENT_DIR_PARTS
            for part in path.relative_to(RUNTIME).parts
        )
    )


class MarsUnderwritingSkillTests(unittest.TestCase):
    def _render(
        self, renderer: Path, fixture: Path, output: Path
    ) -> subprocess.CompletedProcess[str]:
        if renderer == UNDERWRITING_RENDERER:
            # The renderer resolves portable evidence paths next to its input.
            # Stage exactly the artifact named by technical_evidence_ref —
            # no fallback, no as_of rewrite — so a missing reference or a
            # timestamp drift fails the test instead of being patched over.
            with tempfile.TemporaryDirectory(prefix="mars-underwriting-input-") as staging:
                staging_path = Path(staging)
                staged_fixture = json.loads(fixture.read_text(encoding="utf-8"))
                staged_input = staging_path / fixture.name
                staged_input.write_text(
                    json.dumps(staged_fixture, ensure_ascii=False), encoding="utf-8"
                )
                evidence_ref = staged_fixture.get("technical_evidence_ref")
                evidence_rel = "technical-evidence.json"
                if isinstance(evidence_ref, dict) and isinstance(
                    evidence_ref.get("artifact_path"), str
                ):
                    evidence_rel = evidence_ref["artifact_path"]
                candidate = Path(evidence_rel)
                portable = (
                    not candidate.is_absolute()
                    and "\\" not in evidence_rel
                    and ":" not in evidence_rel
                    and ".." not in candidate.parts
                )
                source_evidence = UNDERWRITING_FIXTURE.parent / candidate
                if portable and source_evidence.is_file():
                    target = staging_path / candidate
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source_evidence.read_bytes())
                return subprocess.run(
                    [sys.executable, str(renderer), "--input", str(staged_input), "--output", str(output)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
        return subprocess.run(
            [sys.executable, str(renderer), "--input", str(fixture), "--output", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_runtime_package_is_discoverable_and_lightweight(self) -> None:
        self.assertFalse((ROOT / "SKILL.md").exists())
        manifest = json.loads((RUNTIME / "mars-skills.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("collection"), "Mars Research Assistant")
        self.assertEqual({item["id"] for item in manifest["skills"]}, EXPECTED_SKILLS)
        display_names = {item["id"]: item["display_name"] for item in manifest["skills"]}
        self.assertEqual(display_names["deep-equity-research"], "深度研究")
        self.assertEqual(display_names["investment-analysis"], "投研分析")
        files = runtime_files()
        self.assertLessEqual(len(files), 80)
        self.assertLessEqual(sum(path.stat().st_size for path in files), 3 << 19)
        root_skill = (RUNTIME / "SKILL.md").read_text(encoding="utf-8")
        for marker in (
            "npx skills add archerthegoat/mars-research-assistant",
            "--skill mars-research-assistant",
            "--agent codex",
            "--global",
            "--copy",
        ):
            self.assertIn(marker, root_skill)

    def test_skills_follow_the_concise_skill_structure(self) -> None:
        for identifier in EXPECTED_SKILLS | {"mars-research-assistant"}:
            skill_path = (
                RUNTIME / "SKILL.md"
                if identifier == "mars-research-assistant"
                else RUNTIME / "skills" / identifier / "SKILL.md"
            )
            with self.subTest(identifier=identifier):
                text = skill_path.read_text(encoding="utf-8")
                self.assertLessEqual(len(text.splitlines()), 100)
                self.assertIn("description:", text)

    def test_textual_skills_expose_a_non_overwriting_local_markdown_contract(self) -> None:
        expected = {
            "directory": "mars-research",
            "format": "markdown",
            "unique_name_required": True,
            "overwrite": "forbidden",
        }
        for identifier in EXPECTED_SKILLS:
            with self.subTest(identifier=identifier):
                capability = json.loads(
                    (RUNTIME / "skills" / identifier / "capability.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(capability.get("local_artifact_contract"), expected)

    def test_drive_writeback_exposes_only_workbench_operations(self) -> None:
        capability = json.loads(
            (RUNTIME / "skills" / "drive-writeback" / "capability.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            capability["supported_operations"],
            ["initialize_workbench", "workbench_write"],
        )
        payload = json.dumps(capability, ensure_ascii=False)
        for retired in ("archive_completed_research", "archive_contract", "archive_routes"):
            self.assertNotIn(retired, payload)
        skill_text = (RUNTIME / "skills" / "drive-writeback" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("归档", skill_text)
        self.assertIn("投研工作台", skill_text)

    def test_equity_snapshot_renders_required_data_and_recent_updates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            output = Path(temporary) / "mars-research" / "snapshot.md"
            started = perf_counter()
            result = self._render(SNAPSHOT_RENDERER, SNAPSHOT_FIXTURE, output)
            elapsed = perf_counter() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(elapsed, 1.0)
            rendered = output.read_text(encoding="utf-8")
        for marker in (
            "# 个股快览：TEST",
            "## 发行人身份",
            "## 关键公开数据",
            "## 最近 30 天公司相关公告或新闻",
            "as_of：",
            "## 数据缺口",
        ):
            self.assertIn(marker, rendered)

    def test_equity_snapshot_never_overwrites_and_requires_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            temporary_path = Path(temporary)
            output = temporary_path / "mars-research" / "snapshot.md"
            first = self._render(SNAPSHOT_RENDERER, SNAPSHOT_FIXTURE, output)
            second = self._render(SNAPSHOT_RENDERER, SNAPSHOT_FIXTURE, output)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("File exists", second.stderr)

            fixture = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
            fixture["identity"] = {"status": "unavailable", "reason": "代码有歧义。"}
            ambiguous = temporary_path / "ambiguous.json"
            ambiguous.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            blocked_output = temporary_path / "mars-research" / "blocked.md"
            blocked = self._render(SNAPSHOT_RENDERER, ambiguous, blocked_output)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("not uniquely verified", blocked.stderr)
            self.assertFalse(blocked_output.exists())

            fixture = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
            fixture["identity"]["verified"]["ticker"] = "OTHER"
            mismatch = temporary_path / "mismatched-identity.json"
            mismatch.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            mismatched_output = temporary_path / "mars-research" / "mismatched.md"
            mismatched = self._render(SNAPSHOT_RENDERER, mismatch, mismatched_output)
            self.assertNotEqual(mismatched.returncode, 0)
            self.assertIn("does not match", mismatched.stderr)
            self.assertFalse(mismatched_output.exists())

            cases = (
                ("unrelated", lambda item: item.update({"issuer": "Unrelated Issuer"}), "not directly related"),
                ("directive", lambda item: item.update({"value": "建议增持该股票。"}), "trade directive"),
                ("invalid-as-of", lambda item: item["source"].update({"as_of": "not-a-timestamp"}), "complete timestamp"),
                ("naive-as-of", lambda item: item["source"].update({"as_of": "2026-07-30T12:00:00"}), "timezone"),
                ("same-day-future-as-of", lambda item: item["source"].update({"as_of": "2026-07-30T12:00:01Z"}), "after research as_of"),
                ("future-as-of", lambda item: item["source"].update({"as_of": "2026-07-31T00:00:00Z"}), "after research as_of"),
            )
            for name, mutate, expected_error in cases:
                with self.subTest(name=name):
                    fixture = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
                    target = (
                        fixture["recent_company_updates"][0]
                        if name == "unrelated"
                        else fixture["key_public_data"][0]
                    )
                    mutate(target)
                    path = temporary_path / f"{name}.json"
                    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
                    output = temporary_path / "mars-research" / f"{name}.md"
                    result = self._render(SNAPSHOT_RENDERER, path, output)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected_error, result.stderr)
                    self.assertFalse(output.exists())

    def test_instrument_research_fixtures_follow_the_current_contract(self) -> None:
        # 代表性快览 fixture 直接走当前渲染合同（全时区 as_of、身份核验、
        # 三项关键数据、30 天动态窗口），防止 fixture 再次漂移。
        fixtures = ROOT / "tests" / "fixtures"
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            temporary_path = Path(temporary)
            for name, marker in (
                ("instrument-research-primary.json", "# 个股快览：NVDA"),
                ("instrument-research-non-us.json", "# 个股快览：7203.T"),
            ):
                with self.subTest(fixture=name):
                    output = temporary_path / f"{name}.md"
                    result = self._render(SNAPSHOT_RENDERER, fixtures / name, output)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rendered = output.read_text(encoding="utf-8")
                    for section in (
                        marker,
                        "## 发行人身份",
                        "## 关键公开数据",
                        "## 最近 30 天公司相关公告或新闻",
                        "## 数据缺口",
                    ):
                        self.assertIn(section, rendered)
            with self.subTest(fixture="instrument-research-evidence-gap.json"):
                blocked_output = temporary_path / "evidence-gap.md"
                blocked = self._render(
                    SNAPSHOT_RENDERER,
                    fixtures / "instrument-research-evidence-gap.json",
                    blocked_output,
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("not uniquely verified", blocked.stderr)
                self.assertFalse(blocked_output.exists())

    def test_equity_renderers_refuse_skill_runtime_output_paths(self) -> None:
        cases = (
            (SNAPSHOT_RENDERER, SNAPSHOT_FIXTURE, RUNTIME / "blocked-snapshot.md"),
            (
                UNDERWRITING_RENDERER,
                UNDERWRITING_FIXTURE,
                RUNTIME / "skills" / "deep-equity-research" / "blocked-underwriting.md",
            ),
        )
        for renderer, fixture, output in cases:
            with self.subTest(renderer=renderer.name):
                try:
                    result = self._render(renderer, fixture, output)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Skill runtime package", result.stderr)
                    self.assertFalse(output.exists())
                finally:
                    output.unlink(missing_ok=True)

    def test_underwriting_renders_nine_chapters_identity_and_trade_conclusion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            output = Path(temporary) / "mars-research" / "underwriting.md"
            started = perf_counter()
            result = self._render(UNDERWRITING_RENDERER, UNDERWRITING_FIXTURE, output)
            elapsed = perf_counter() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(elapsed, 1.0)
            rendered = output.read_text(encoding="utf-8")
        self.assertIn("# 深度研究：CLEAN.US", rendered)
        for number, title in enumerate(UNDERWRITING_CHAPTERS, 1):
            self.assertIn(f"## {number}. {title}", rendered)
        for marker in (
            "issuer-cleanco",
            "case-underwriting-001",
            "首次承保",
        ):
            self.assertIn(marker, rendered)
        for directive in ("买入", "卖出", "增持", "减持", "加仓", "减仓", "做空"):
            self.assertNotIn(directive, rendered)

    def test_underwriting_records_baseline_gap_instead_of_fabricating(self) -> None:
        fixture_path = ROOT / "tests" / "fixtures" / "underwriting-inputs-short-baseline.json"
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            output = Path(temporary) / "mars-research" / "underwriting.md"
            result = self._render(UNDERWRITING_RENDERER, fixture_path, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
        self.assertIn("## 9. 来源、数据对账、时间戳、假设与数据缺口", rendered)
        self.assertIn("基线", rendered)

    def test_underwriting_earnings_update_without_prior_model_degrades(self) -> None:
        fixture_path = ROOT / "tests" / "fixtures" / "underwriting-inputs-earnings-no-prior.json"
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            output = Path(temporary) / "mars-research" / "underwriting.md"
            result = self._render(UNDERWRITING_RENDERER, fixture_path, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
        self.assertIn("自动降级为首次承保", rendered)

    def test_underwriting_rejects_mismatched_identity_sources_and_directives(self) -> None:
        cases = (
            ("identity", lambda fixture: fixture["issuer_identity"]["verified"].update({"ticker": "OTHER"}), "does not match"),
            ("directive", lambda fixture: fixture["sections"]["research_scope_hypothesis_trade_conclusion"][0].update({"statement": "建议买入该股票。"}), "trade directive"),
            ("future-as-of", lambda fixture: fixture["sections"]["company_business_model_value_drivers"][0]["source"].update({"as_of": "2026-08-02T00:00:00Z"}), "after research as_of"),
            ("case-mismatch", lambda fixture: fixture["valuation"]["identity"].update({"case_id": "other-case"}), "case_id"),
        )
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            temporary_path = Path(temporary)
            for name, mutate, expected_error in cases:
                with self.subTest(name=name):
                    fixture = json.loads(UNDERWRITING_FIXTURE.read_text(encoding="utf-8"))
                    mutate(fixture)
                    path = temporary_path / f"{name}.json"
                    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
                    output = temporary_path / "mars-research" / f"{name}.md"
                    result = self._render(UNDERWRITING_RENDERER, path, output)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected_error, result.stderr)
                    self.assertFalse(output.exists())

    def test_offline_verifier_accepts_the_runtime_contract(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/verify_mars_skills.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Mars Skills contract ok", result.stdout)

    def test_red_bundle_contains_only_the_runtime_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mars-test-") as temporary:
            bundle = Path(temporary) / "mars.zip"
            result = subprocess.run(
                [sys.executable, "scripts/build_red_upload_bundle.py", "--output", str(bundle)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
        self.assertIn("mars-research-assistant/SKILL.md", names)
        self.assertIn(
            "mars-research-assistant/skills/deep-equity-research/SKILL.md", names
        )
        self.assertIn(
            "mars-research-assistant/skills/investment-analysis/SKILL.md", names
        )
        self.assertNotIn("mars-research-assistant/tests/test_mars_skills_suite.py", names)
        self.assertNotIn("mars-research-assistant/README.md", names)

    @unittest.skipUnless(
        os.environ.get("MARS_RUN_NPX_INTEGRATION") == "1",
        "set MARS_RUN_NPX_INTEGRATION=1 to run the local Skills CLI integration test",
    )
    def test_skills_cli_copies_only_the_runtime_package_into_a_local_project(self) -> None:
        npx = shutil.which("npx")
        self.assertIsNotNone(npx, "npx is required for the public Skills CLI entrypoint")
        with tempfile.TemporaryDirectory(prefix="mars-npx-") as temporary:
            project = Path(temporary) / "consumer"
            setup = subprocess.run(
                ["git", "init", "--quiet", str(project)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
            listed = subprocess.run(
                [npx, "skills", "add", str(ROOT), "--list"],
                cwd=project,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
            discovery = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", listed.stdout)
            self.assertIn("Found 1 skill", discovery)
            self.assertEqual(discovery.count("mars-research-assistant"), 1)
            result = subprocess.run(
                [
                    npx,
                    "skills",
                    "add",
                    str(ROOT),
                    "--skill",
                    "mars-research-assistant",
                    "--agent",
                    "codex",
                    "--copy",
                    "--yes",
                ],
                cwd=project,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            installed = project / ".agents" / "skills" / "mars-research-assistant"
            self.assertTrue(installed.is_dir())
            expected_files = {
                path.relative_to(RUNTIME).as_posix() for path in runtime_files()
            }
            actual_files = {
                path.relative_to(installed).as_posix()
                for path in installed.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual_files, expected_files)


if __name__ == "__main__":
    unittest.main()
