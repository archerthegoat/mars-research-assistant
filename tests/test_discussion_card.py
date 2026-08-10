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
PACKAGE = ROOT / "skills" / "mars-research-assistant" / "skills" / "investment-analysis"
SCRIPT = PACKAGE / "scripts" / "render_discussion_card.py"
SKILL_MD = PACKAGE / "SKILL.md"
CAPABILITY = PACKAGE / "capability.json"
FIXTURES = ROOT / "tests" / "fixtures"


def _run(
    arguments: list[str], cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
    )


class DiscussionCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fixture(self, name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def _input_path(self, payload: dict, stem: str = "discussion-inputs") -> Path:
        path = self.tmp / f"{stem}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def _render(
        self,
        payload: dict,
        stem: str = "discussion-card",
        with_json: bool = False,
        cwd: Path = ROOT,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        input_path = self._input_path(payload)
        md_path = self.tmp / f"{stem}.md"
        json_path = self.tmp / f"{stem}.json"
        arguments = ["--input", str(input_path), "--output", str(md_path)]
        if with_json:
            arguments.extend(["--json", str(json_path)])
        return _run(arguments, cwd=cwd), md_path, json_path

    def test_full_fixture_markdown_sections(self) -> None:
        result, md_path, _ = self._render(self._fixture("discussion-inputs-full.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        markdown = md_path.read_text(encoding="utf-8")
        self.assertIn("# 投研讨论卡：case-test-001", markdown)
        for section in (
            "## 输入声明",
            "## 新证据",
            "## 假设变化",
            "## 论点状态",
            "## 反方论证",
            "## 待验证事项",
            "## 置信度",
            "## 方案 revision 提议",
            "## 引用 artifact",
            "## 升级提议",
        ):
            self.assertIn(section, markdown)
        self.assertIn("plan-case-test-001", markdown)
        self.assertIn("base_revision：2", markdown)
        self.assertIn("需经 Drive 工作台 confirm 才生效", markdown)
        self.assertIn("tests/fixtures/valuation-full.json", markdown)
        self.assertIn("case_id=case-test-001", markdown)
        self.assertIn("强化（strengthened）", markdown)

    def test_full_fixture_json_output(self) -> None:
        result, _, json_path = self._render(
            self._fixture("discussion-inputs-full.json"), with_json=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        card = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(card["identity"]["case_id"], "case-test-001")
        self.assertEqual(
            card["engine"],
            "skills/investment-analysis/scripts/render_discussion_card.py",
        )
        self.assertEqual(card["engine_version"], "1.0.0")
        self.assertEqual(card["card_version"], "v1.0.3-card-1")
        self.assertEqual(card["thesis_status"], "strengthened")
        self.assertEqual(len(card["new_evidence"]), 2)
        self.assertEqual(card["plan_revision"]["plan_id"], "plan-case-test-001")
        self.assertEqual(card["plan_revision"]["base_revision"], 2)
        self.assertEqual(
            card["artifact_refs"][0]["path"], "tests/fixtures/valuation-full.json"
        )
        self.assertEqual(
            card["artifact_refs"][0]["identity"]["case_id"], "case-test-001"
        )

    def test_empty_fixture_maintains_plan(self) -> None:
        result, md_path, _ = self._render(self._fixture("discussion-inputs-empty.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        markdown = md_path.read_text(encoding="utf-8")
        self.assertIn("维持原方案/暂无动作", markdown)
        self.assertIn("维持（unchanged）", markdown)

    def test_schema_version_bool_is_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-empty.json")
        payload["schema_version"] = True
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version must be 1", result.stderr)

    def test_artifact_ref_top_level_schema_bool_is_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        reference = self._fixture("valuation-full.json")
        reference["schema_version"] = True
        ref_path = self.tmp / "bool-schema-ref.json"
        ref_path.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
        payload["artifact_refs"] = [ref_path.name]
        result, _, _ = self._render(payload, cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version", result.stderr)

    def test_empty_inputs_declared_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-empty.json")
        payload["inputs_declared"] = {
            "artifact_paths": [],
            "drive_doc_id": "",
            "case_id": "",
        }
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不扫描全库", result.stderr)

    def test_inputs_declared_case_id_must_match_card_identity(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["inputs_declared"]["case_id"] = "case-other-999"
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("case_id", result.stderr)
        # 一致时不报错。
        payload = self._fixture("discussion-inputs-full.json")
        result, md_path, _ = self._render(payload, stem="discussion-card-match")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("case_id：case-test-001", md_path.read_text(encoding="utf-8"))

    def test_empty_increment_with_strengthened_status_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-empty.json")
        payload["thesis_status"] = "strengthened"
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged", result.stderr)

    def test_missing_artifact_ref_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = ["tests/fixtures/does-not-exist.json"]
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does-not-exist.json", result.stderr)

    def _ref_payload(self, identity: dict) -> str:
        ref = self._fixture("valuation-full.json")
        ref["identity"] = {
            "artifact_version": 1,
            "schema_version": 1,
            **identity,
        }
        path = self.tmp / "ref-artifact.json"
        path.write_text(json.dumps(ref, ensure_ascii=False), encoding="utf-8")
        return "ref-artifact.json"

    def test_artifact_ref_matching_identity_renders(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = [
            self._ref_payload(
                {
                    "issuer_id": "example-issuer",
                    "listing_id": "TEST.US",
                    "case_id": "case-test-001",
                }
            )
        ]
        result, md_path, _ = self._render(payload, cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ref-artifact.json", md_path.read_text(encoding="utf-8"))

    def test_artifact_ref_case_id_mismatch_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = [
            self._ref_payload(
                {
                    "issuer_id": "example-issuer",
                    "listing_id": "TEST.US",
                    "case_id": "case-other-999",
                }
            )
        ]
        result, _, _ = self._render(payload, cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("case_id", result.stderr)

    def test_artifact_ref_issuer_id_mismatch_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = [
            self._ref_payload(
                {
                    "issuer_id": "other-issuer",
                    "listing_id": "TEST.US",
                    "case_id": "case-test-001",
                }
            )
        ]
        result, _, _ = self._render(payload, cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("issuer_id", result.stderr)

    def test_artifact_ref_ah_case_accepted(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["identity"]["listing_id"] = "TEST.SH"
        payload["artifact_refs"] = [
            self._ref_payload(
                {
                    "issuer_id": "example-issuer",
                    "listing_id": "TEST.HK",
                    "case_id": "case-test-001",
                }
            )
        ]
        result, md_path, _ = self._render(payload, cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        markdown = md_path.read_text(encoding="utf-8")
        self.assertIn("listing_id=TEST.HK", markdown)
        self.assertIn("case_id=case-test-001", markdown)

    def test_artifact_ref_non_ah_listing_mismatch_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = [
            self._ref_payload(
                {
                    "issuer_id": "example-issuer",
                    "listing_id": "TEST.HK",
                    "case_id": "case-test-001",
                }
            )
        ]
        result, _, _ = self._render(payload, cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("listing_id", result.stderr)

    def test_artifact_ref_incomplete_identity_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = [
            self._ref_payload(
                {
                    "issuer_id": "example-issuer",
                    "case_id": "case-test-001",
                    "artifact_version": None,
                }
            )
        ]
        result, _, _ = self._render(payload, cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("identity", result.stderr)
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = [
            self._ref_payload(
                {
                    "issuer_id": "example-issuer",
                    "listing_id": "TEST.US",
                    "case_id": "case-test-001",
                    "artifact_version": 2,
                }
            )
        ]
        result, _, _ = self._render(payload, cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact_version", result.stderr)

    def test_absolute_path_rejected(self) -> None:
        absolute = str(FIXTURES / "valuation-full.json")
        payload = self._fixture("discussion-inputs-full.json")
        payload["inputs_declared"]["artifact_paths"] = [absolute]
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portable relative path", result.stderr)
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = [absolute]
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portable relative path", result.stderr)

    def test_dotdot_path_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["inputs_declared"]["artifact_paths"] = [
            "tests/../tests/fixtures/valuation-full.json"
        ]
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not contain '..'", result.stderr)
        payload = self._fixture("discussion-inputs-full.json")
        payload["artifact_refs"] = ["tests/fixtures/../fixtures/valuation-full.json"]
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not contain '..'", result.stderr)

    def test_error_output_never_echoes_private_absolute_path(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        private = self.tmp / "private-ref-artifact.json"
        private.write_text(
            json.dumps(self._fixture("valuation-full.json"), ensure_ascii=False),
            encoding="utf-8",
        )
        payload["artifact_refs"] = [str(private)]
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(str(self.tmp), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_trade_directive_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["new_evidence"][0]["statement"] = "建议买入该公司股票。"
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trade directive", result.stderr)

    def test_short_selling_advice_rejected(self) -> None:
        cases = {
            "new_evidence statement 做空": lambda payload: payload["new_evidence"][
                0
            ].update(statement="建议做空该公司股票。"),
            "new_evidence statement short": lambda payload: payload["new_evidence"][
                0
            ].update(statement="consider a short position"),
            "assumption_changes 沽空": lambda payload: payload.update(
                assumption_changes=["假设可沽空对冲。"]
            ),
            "counter_arguments 卖空": lambda payload: payload.update(
                counter_arguments=["反方建议卖空。"]
            ),
            "escalation_proposal 做空": lambda payload: payload.update(
                escalation_proposal="升级建议：做空。"
            ),
            "plan_revision change_summary short": lambda payload: payload[
                "plan_revision"
            ].update(change_summary="add a short leg"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                payload = self._fixture("discussion-inputs-full.json")
                mutate(payload)
                result, md_path, _ = self._render(payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("trade directive", result.stderr)
                self.assertFalse(md_path.exists())

    def test_future_source_as_of_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-full.json")
        payload["new_evidence"][0]["source"]["as_of"] = "2026-08-04T00:00:00Z"
        result, _, _ = self._render(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("after research as_of", result.stderr)

    def test_duplicate_output_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-empty.json")
        input_path = self._input_path(payload)
        md_path = self.tmp / "discussion-card.md"
        arguments = ["--input", str(input_path), "--output", str(md_path)]
        first = _run(arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = _run(arguments)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("File exists", second.stderr)

    def test_output_inside_runtime_package_rejected(self) -> None:
        payload = self._fixture("discussion-inputs-empty.json")
        input_path = self._input_path(payload)
        result = _run(
            [
                "--input",
                str(input_path),
                "--output",
                str(PACKAGE / "discussion-card.md"),
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime package", result.stderr)

    def test_capability_matches_skill_policy_block(self) -> None:
        capability = json.loads(CAPABILITY.read_text(encoding="utf-8"))
        match = re.search(
            r"```mars-skill-policy\n(.*?)\n```",
            SKILL_MD.read_text(encoding="utf-8"),
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        policy = json.loads(match.group(1))
        self.assertEqual(
            policy,
            {
                "delivery": capability["delivery"],
                "forbidden_effects": capability["forbidden_effects"],
            },
        )

    def test_skill_md_within_line_budget(self) -> None:
        lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 100)


if __name__ == "__main__":
    unittest.main()
