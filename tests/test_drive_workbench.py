from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "mars-research-assistant"
    / "skills"
    / "drive-writeback"
    / "scripts"
    / "drive_workbench.py"
)
WORKBENCH_REL = Path("mars-research") / "drive-workbench"


def _run(workspace: Path, *args: str, expect_ok: bool = True):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--workspace", str(workspace)],
        capture_output=True,
        text=True,
    )
    if expect_ok:
        if proc.returncode != 0:
            raise AssertionError(f"CLI failed unexpectedly: {proc.stderr}")
        return json.loads(proc.stdout)
    if proc.returncode == 0:
        raise AssertionError(f"CLI should have failed but succeeded: {proc.stdout}")
    if not proc.stderr.strip():
        raise AssertionError("CLI failure must explain itself on stderr")
    return proc


def _write_payload(
    tmp: Path,
    name: str = "plan.json",
    marker: str | None = None,
    case_id: str = "case-demo",
) -> Path:
    payload = {
        "identity": {
            "issuer_id": "issuer-demo",
            "listing_id": "0700.HK",
            "case_id": case_id,
            "artifact_version": 1,
            "schema_version": 1,
        },
        "status": "entry_plan",
        "direction": "long_only",
        "entry_plan": {"zone": {"low": 100, "high": 110}},
    }
    if marker is not None:
        payload["full_research_body"] = marker
    path = tmp / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class DriveWorkbenchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.workbench = self.tmp / WORKBENCH_REL

    def _init_case(self, case_id: str = "case-demo") -> dict:
        return _run(
            self.tmp,
            "init-case",
            "--case-id", case_id,
            "--issuer-id", "issuer-demo",
            "--listing-id", "0700.HK",
            "--listing-id", "600519.SS",
        )

    def _propose(
        self,
        payload: Path,
        *,
        base: int,
        case_id: str = "case-demo",
        plan_id: str = "plan-a",
        reason: str = "首次方案",
    ) -> dict:
        return _run(
            self.tmp,
            "propose",
            "--case-id", case_id,
            "--section", "current_plan",
            "--payload", str(payload),
            "--plan-id", plan_id,
            "--base-revision", str(base),
            "--change-reason", reason,
        )

    def _doc(self, case_id: str = "case-demo") -> dict:
        return json.loads(
            (self.workbench / "documents" / f"{case_id}.json").read_text(
                encoding="utf-8"
            )
        )

    def _proposal(self, proposal_id: str) -> dict:
        return json.loads(
            (self.workbench / "proposals" / f"{proposal_id}.json").read_text(
                encoding="utf-8"
            )
        )

    def _make_conflict(self, choice_case: str, marker: str | None = None) -> str:
        """init → confirm base0 → stale propose base0 → confirm → conflict."""
        self._init_case(choice_case)
        payload1 = _write_payload(
            self.tmp, f"{choice_case}-p1.json", case_id=choice_case
        )
        first = self._propose(payload1, base=0, case_id=choice_case)
        _run(self.tmp, "confirm", "--proposal-id", first["proposal_id"])
        payload2 = _write_payload(
            self.tmp, f"{choice_case}-p2.json", marker=marker, case_id=choice_case
        )
        stale = self._propose(
            payload2, base=0, case_id=choice_case, reason="过期 base 的第二个方案"
        )
        result = _run(self.tmp, "confirm", "--proposal-id", stale["proposal_id"])
        self.assertEqual(result["status"], "conflict")
        return stale["proposal_id"]

    # --- init-case -----------------------------------------------------

    def test_init_case_creates_structure_and_is_idempotent(self) -> None:
        result = self._init_case()
        self.assertEqual(result["status"], "created")
        manifest = json.loads(
            (self.workbench / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(len(manifest["cases"]), 1)
        case = manifest["cases"][0]
        self.assertEqual(case["case_id"], "case-demo")
        self.assertEqual(case["issuer_id"], "issuer-demo")
        self.assertEqual(case["listing_ids"], ["0700.HK", "600519.SS"])
        self.assertEqual(case["doc"], "documents/case-demo.json")
        self.assertIn("created_as_of", case)

        document = self._doc()
        self.assertEqual(document["head_revision"], 0)
        for section in ("idea_log", "current_plan", "decision_log", "review_log"):
            self.assertEqual(document["sections"][section]["entries"], [])
        self.assertEqual(document["user_sections"], {})

        doc_before = (self.workbench / "documents" / "case-demo.json").read_text(
            encoding="utf-8"
        )
        again = self._init_case()
        self.assertEqual(again["status"], "existing")
        doc_after = (self.workbench / "documents" / "case-demo.json").read_text(
            encoding="utf-8"
        )
        self.assertEqual(doc_before, doc_after)
        manifest_again = json.loads(
            (self.workbench / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest_again["cases"]), 1)

    def test_init_case_creates_no_proposals(self) -> None:
        self._init_case()
        self.assertEqual(list((self.workbench / "proposals").iterdir()), [])

    # --- propose / confirm / read chain --------------------------------

    def test_propose_does_not_write_document(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        before = (self.workbench / "documents" / "case-demo.json").read_text(
            encoding="utf-8"
        )
        result = self._propose(payload, base=0)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["document"], "documents/case-demo.json")
        self.assertEqual(result["section"], "current_plan")
        self.assertEqual(result["plan_id"], "plan-a")
        self.assertEqual(result["base_revision"], 0)
        after = (self.workbench / "documents" / "case-demo.json").read_text(
            encoding="utf-8"
        )
        self.assertEqual(before, after)
        self.assertEqual(self._proposal(result["proposal_id"])["status"], "pending")

    def test_propose_requires_existing_payload(self) -> None:
        self._init_case()
        _run(
            self.tmp,
            "propose",
            "--case-id", "case-demo",
            "--section", "current_plan",
            "--payload", str(self.tmp / "missing.json"),
            "--plan-id", "plan-a",
            "--base-revision", "0",
            "--change-reason", "不存在的 payload",
            expect_ok=False,
        )
        self.assertEqual(list((self.workbench / "proposals").iterdir()), [])

    def test_propose_binds_payload_issuer_and_listing_to_manifest(self) -> None:
        self._init_case()
        for field, value in (("issuer_id", "issuer-other"), ("listing_id", "9999.HK")):
            with self.subTest(field=field):
                payload = _write_payload(self.tmp, f"mismatch-{field}.json")
                data = json.loads(payload.read_text(encoding="utf-8"))
                data["identity"][field] = value
                payload.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                _run(
                    self.tmp,
                    "propose",
                    "--case-id", "case-demo",
                    "--section", "current_plan",
                    "--payload", str(payload),
                    "--plan-id", "plan-a",
                    "--base-revision", "0",
                    "--change-reason", "身份不匹配",
                    expect_ok=False,
                )
        self.assertEqual(list((self.workbench / "proposals").iterdir()), [])

    def test_propose_confirm_read_chain(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        proposal = self._propose(payload, base=0)
        confirmed = _run(self.tmp, "confirm", "--proposal-id", proposal["proposal_id"])
        self.assertEqual(confirmed["status"], "applied")
        self.assertEqual(confirmed["revision"], 1)
        self.assertEqual(confirmed["parent_revision"], 0)
        self.assertEqual(confirmed["head_revision"], 1)
        receipt = json.loads(
            (
                self.workbench / "proposals" / f"{proposal['proposal_id']}-receipt.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["proposal_id"], proposal["proposal_id"])
        self.assertEqual(receipt["revision"], 1)
        self.assertEqual(self._proposal(proposal["proposal_id"])["status"], "applied")

        read = _run(self.tmp, "read", "--case-id", "case-demo")
        self.assertEqual(read["head_revision"], 1)
        plan_section = read["sections"]["current_plan"]
        self.assertEqual(plan_section["entry_count"], 1)
        self.assertEqual(plan_section["entries"][0]["plan_id"], "plan-a")
        self.assertEqual(plan_section["entries"][0]["revision"], 1)
        self.assertEqual(read["sections"]["idea_log"]["entry_count"], 0)
        self.assertTrue(read["user_sections_preserved"])

    def test_revision_increments_and_records_parent(self) -> None:
        self._init_case()
        first = self._propose(_write_payload(self.tmp, "p1.json"), base=0)
        _run(self.tmp, "confirm", "--proposal-id", first["proposal_id"])
        second = self._propose(
            _write_payload(self.tmp, "p2.json"), base=1, reason="第二次修订"
        )
        confirmed = _run(self.tmp, "confirm", "--proposal-id", second["proposal_id"])
        self.assertEqual(confirmed["revision"], 2)
        self.assertEqual(confirmed["parent_revision"], 1)
        entries = self._doc()["sections"]["current_plan"]["entries"]
        self.assertEqual([e["revision"] for e in entries], [1, 2])
        self.assertEqual([e["parent_revision"] for e in entries], [0, 1])

    def test_confirm_unknown_or_consumed_proposal_fails(self) -> None:
        self._init_case()
        _run(
            self.tmp, "confirm", "--proposal-id", "prop-nonexistent", expect_ok=False
        )
        proposal = self._propose(_write_payload(self.tmp), base=0)
        _run(self.tmp, "confirm", "--proposal-id", proposal["proposal_id"])
        _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            expect_ok=False,
        )

    # --- parent-revision conflict --------------------------------------

    def test_conflict_summary_and_keep_local(self) -> None:
        proposal_id = self._make_conflict("case-keep-local")
        conflict = json.loads(
            (self.workbench / "proposals" / f"{proposal_id}-conflict.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(conflict["base_revision"], 0)
        self.assertEqual(conflict["head_revision"], 1)
        self.assertEqual(conflict["local_head_entry"]["revision"], 1)
        self.assertEqual(conflict["proposed_entry"]["revision"], 1)
        self.assertEqual(
            conflict["options"],
            ["keep_local", "keep_incoming", "merge", "new_plan"],
        )
        self.assertEqual(self._proposal(proposal_id)["status"], "conflict")
        self.assertEqual(self._doc("case-keep-local")["head_revision"], 1)
        self.assertEqual(
            len(self._doc("case-keep-local")["sections"]["current_plan"]["entries"]), 1
        )

        resolved = _run(
            self.tmp,
            "resolve-conflict",
            "--proposal-id", proposal_id,
            "--choice", "keep_local",
        )
        self.assertEqual(resolved["status"], "abandoned")
        self.assertEqual(self._doc("case-keep-local")["head_revision"], 1)
        self.assertEqual(
            len(self._doc("case-keep-local")["sections"]["current_plan"]["entries"]), 1
        )
        self.assertEqual(self._proposal(proposal_id)["status"], "abandoned")

    def test_conflict_keep_incoming_applies(self) -> None:
        proposal_id = self._make_conflict("case-keep-incoming")
        resolved = _run(
            self.tmp,
            "resolve-conflict",
            "--proposal-id", proposal_id,
            "--choice", "keep_incoming",
        )
        self.assertEqual(resolved["status"], "applied")
        self.assertEqual(resolved["revision"], 2)
        self.assertEqual(resolved["parent_revision"], 1)
        entries = self._doc("case-keep-incoming")["sections"]["current_plan"]["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1]["change_reason"], "过期 base 的第二个方案")
        self.assertEqual(self._proposal(proposal_id)["status"], "applied")

    def test_conflict_new_plan_requires_id_and_starts_at_revision_1(self) -> None:
        proposal_id = self._make_conflict("case-new-plan")
        _run(
            self.tmp,
            "resolve-conflict",
            "--proposal-id", proposal_id,
            "--choice", "new_plan",
            expect_ok=False,
        )
        resolved = _run(
            self.tmp,
            "resolve-conflict",
            "--proposal-id", proposal_id,
            "--choice", "new_plan",
            "--new-plan-id", "plan-b",
        )
        self.assertEqual(resolved["status"], "applied")
        self.assertEqual(resolved["plan_id"], "plan-b")
        self.assertEqual(resolved["revision"], 1)
        self.assertIsNone(resolved["parent_revision"])
        entries = self._doc("case-new-plan")["sections"]["current_plan"]["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1]["plan_id"], "plan-b")
        self.assertEqual(entries[-1]["revision"], 1)
        self.assertIsNone(entries[-1]["parent_revision"])

    def test_conflict_merge_fails_closed_without_payload(self) -> None:
        proposal_id = self._make_conflict("case-merge")
        _run(
            self.tmp,
            "resolve-conflict",
            "--proposal-id", proposal_id,
            "--choice", "merge",
            expect_ok=False,
        )
        self.assertEqual(self._doc("case-merge")["head_revision"], 1)
        merged_payload = _write_payload(
            self.tmp, "case-merge-merged.json", case_id="case-merge"
        )
        resolved = _run(
            self.tmp,
            "resolve-conflict",
            "--proposal-id", proposal_id,
            "--choice", "merge",
            "--payload", str(merged_payload),
        )
        self.assertEqual(resolved["status"], "applied")
        self.assertEqual(resolved["revision"], 2)
        entries = self._doc("case-merge")["sections"]["current_plan"]["entries"]
        self.assertIn("merged", entries[-1]["change_reason"])
        self.assertIn("过期 base 的第二个方案", entries[-1]["change_reason"])

    # --- outbox / retry -------------------------------------------------

    def test_outbox_pending_sync_and_retry_applies(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        queued = _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            "--drive-available", "false",
        )
        self.assertEqual(queued["status"], "pending_sync")
        outbox_path = self.workbench / "outbox" / f"{proposal['proposal_id']}.json"
        self.assertTrue(outbox_path.is_file())
        self.assertEqual(
            json.loads(outbox_path.read_text(encoding="utf-8"))["status"],
            "pending_sync",
        )
        self.assertEqual(self._doc()["head_revision"], 0)
        self.assertEqual(self._doc()["sections"]["current_plan"]["entries"], [])

        retried = _run(self.tmp, "retry", "--outbox-id", proposal["proposal_id"])
        self.assertEqual(retried["status"], "applied")
        self.assertEqual(retried["revision"], 1)
        self.assertFalse(outbox_path.exists())
        self.assertEqual(self._doc()["head_revision"], 1)

    def test_retry_rechecks_parent_and_enters_conflict(self) -> None:
        self._init_case()
        first = self._propose(_write_payload(self.tmp, "p1.json"), base=0)
        queued = _run(
            self.tmp,
            "confirm",
            "--proposal-id", first["proposal_id"],
            "--drive-available", "false",
        )
        self.assertEqual(queued["status"], "pending_sync")

        second = self._propose(
            _write_payload(self.tmp, "p2.json"), base=0, plan_id="plan-b",
            reason="另一台设备的方案",
        )
        _run(self.tmp, "confirm", "--proposal-id", second["proposal_id"])
        self.assertEqual(self._doc()["head_revision"], 1)

        retried = _run(self.tmp, "retry", "--outbox-id", first["proposal_id"])
        self.assertEqual(retried["status"], "conflict")
        self.assertEqual(
            retried["options"],
            ["keep_local", "keep_incoming", "merge", "new_plan"],
        )
        outbox_path = self.workbench / "outbox" / f"{first['proposal_id']}.json"
        self.assertFalse(outbox_path.exists())
        self.assertEqual(self._proposal(first["proposal_id"])["status"], "conflict")
        conflict_path = (
            self.workbench / "proposals" / f"{first['proposal_id']}-conflict.json"
        )
        self.assertTrue(conflict_path.is_file())
        # Nothing from the queued proposal was written.
        entries = self._doc()["sections"]["current_plan"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["plan_id"], "plan-b")

    def test_retry_with_drive_still_unavailable_keeps_outbox(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            "--drive-available", "false",
        )
        retried = _run(
            self.tmp,
            "retry",
            "--outbox-id", proposal["proposal_id"],
            "--drive-available", "false",
        )
        self.assertEqual(retried["status"], "pending_sync")
        self.assertTrue(
            (self.workbench / "outbox" / f"{proposal['proposal_id']}.json").is_file()
        )
        self.assertEqual(self._doc()["head_revision"], 0)

    # --- user_sections / payload embedding -----------------------------

    def test_user_sections_preserved_across_confirms(self) -> None:
        self._init_case()
        doc_path = self.workbench / "documents" / "case-demo.json"
        document = json.loads(doc_path.read_text(encoding="utf-8"))
        user_sections = {
            "我的笔记": "用户自由内容，skill 不得改写",
            "checklist": ["自定义条目 1", "自定义条目 2"],
        }
        document["user_sections"] = user_sections
        doc_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        first = self._propose(_write_payload(self.tmp, "p1.json"), base=0)
        _run(self.tmp, "confirm", "--proposal-id", first["proposal_id"])
        second = self._propose(_write_payload(self.tmp, "p2.json"), base=1)
        _run(self.tmp, "confirm", "--proposal-id", second["proposal_id"])

        final_doc = self._doc()
        self.assertEqual(final_doc["user_sections"], user_sections)
        self.assertEqual(final_doc["head_revision"], 2)

    def test_payload_content_is_not_embedded(self) -> None:
        self._init_case()
        marker = "PAYLOAD-MARKER-完整研究正文-" * 100
        payload = _write_payload(self.tmp, marker=marker)
        proposal = self._propose(payload, base=0)
        _run(self.tmp, "confirm", "--proposal-id", proposal["proposal_id"])

        doc_text = (
            self.workbench / "documents" / "case-demo.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(marker, doc_text)
        self.assertNotIn("PAYLOAD-MARKER", doc_text)
        self.assertNotIn(str(self.tmp), doc_text)
        entry = self._doc()["sections"]["current_plan"]["entries"][0]
        ref = entry["payload_ref"]
        self.assertEqual(set(ref.keys()), {"artifact_path", "artifact_id", "summary"})
        self.assertTrue(ref["artifact_id"].startswith("sha256:"))
        self.assertEqual(ref["artifact_path"], "plan.json")
        self.assertLessEqual(len(ref["summary"]), 500)

    def test_confirm_rejects_payload_changed_after_propose(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        proposal = self._propose(payload, base=0)
        payload.write_text(
            json.dumps({"status": "watch", "tampered": True}), encoding="utf-8"
        )
        _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self.assertEqual(self._doc()["head_revision"], 0)
        self.assertEqual(self._proposal(proposal["proposal_id"])["status"], "pending")

    # --- portable artifact references -----------------------------------

    def _assert_no_absolute_path(self, text: str) -> None:
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(str(self.tmp.resolve()), text)

    def test_propose_persists_portable_reference(self) -> None:
        self._init_case()
        subdir = self.tmp / "artifacts"
        subdir.mkdir()
        payload = _write_payload(subdir)
        result = self._propose(payload, base=0)
        ref = result["payload_ref"]
        self.assertEqual(ref["artifact_path"], "artifacts/plan.json")
        self.assertTrue(ref["artifact_id"].startswith("sha256:"))
        self._assert_no_absolute_path(json.dumps(result, ensure_ascii=False))
        proposal_text = (
            self.workbench / "proposals" / f"{result['proposal_id']}.json"
        ).read_text(encoding="utf-8")
        self._assert_no_absolute_path(proposal_text)

    def test_propose_rejects_payload_outside_workspace(self) -> None:
        self._init_case()
        with tempfile.TemporaryDirectory() as outside:
            payload = _write_payload(Path(outside))
            _run(
                self.tmp,
                "propose",
                "--case-id", "case-demo",
                "--section", "current_plan",
                "--payload", str(payload),
                "--plan-id", "plan-a",
                "--base-revision", "0",
                "--change-reason", "工作区外的 payload",
                expect_ok=False,
            )
        self.assertEqual(list((self.workbench / "proposals").iterdir()), [])

    def test_confirm_fails_when_payload_missing(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        proposal = self._propose(payload, base=0)
        payload.unlink()
        _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self.assertEqual(self._doc()["head_revision"], 0)
        self.assertEqual(self._proposal(proposal["proposal_id"])["status"], "pending")

    def test_receipt_outbox_and_conflict_contain_no_absolute_path(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        queued = _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            "--drive-available", "false",
        )
        self.assertEqual(queued["status"], "pending_sync")
        self._assert_no_absolute_path(json.dumps(queued, ensure_ascii=False))
        outbox_text = (
            self.workbench / "outbox" / f"{proposal['proposal_id']}.json"
        ).read_text(encoding="utf-8")
        self._assert_no_absolute_path(outbox_text)

        retried = _run(self.tmp, "retry", "--outbox-id", proposal["proposal_id"])
        self.assertEqual(retried["status"], "applied")
        self._assert_no_absolute_path(json.dumps(retried, ensure_ascii=False))
        receipt_text = (
            self.workbench / "proposals" / f"{proposal['proposal_id']}-receipt.json"
        ).read_text(encoding="utf-8")
        self._assert_no_absolute_path(receipt_text)

        conflict_id = self._make_conflict("case-no-abs")
        conflict_text = (
            self.workbench / "proposals" / f"{conflict_id}-conflict.json"
        ).read_text(encoding="utf-8")
        self._assert_no_absolute_path(conflict_text)

        read = _run(self.tmp, "read", "--case-id", "case-demo")
        self._assert_no_absolute_path(json.dumps(read, ensure_ascii=False))
        entry = read["sections"]["current_plan"]["entries"][0]
        self.assertEqual(entry["artifact_path"], "plan.json")

    def test_legacy_absolute_reference_migrates_on_confirm(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        proposal = self._propose(payload, base=0)
        # Simulate an old-format proposal that still stores an absolute path.
        proposal_path = (
            self.workbench / "proposals" / f"{proposal['proposal_id']}.json"
        )
        stored = json.loads(proposal_path.read_text(encoding="utf-8"))
        stored["payload_ref"]["artifact_path"] = str(payload.resolve())
        proposal_path.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        confirmed = _run(self.tmp, "confirm", "--proposal-id", proposal["proposal_id"])
        self.assertEqual(confirmed["status"], "applied")
        entry = self._doc()["sections"]["current_plan"]["entries"][0]
        self.assertEqual(entry["payload_ref"]["artifact_path"], "plan.json")
        receipt_text = (
            self.workbench / "proposals" / f"{proposal['proposal_id']}-receipt.json"
        ).read_text(encoding="utf-8")
        self._assert_no_absolute_path(receipt_text)

    def test_read_masks_unportable_legacy_absolute_path(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        _run(self.tmp, "confirm", "--proposal-id", proposal["proposal_id"])
        # Legacy entry whose absolute path is outside the workspace (verify-only).
        doc_path = self.workbench / "documents" / "case-demo.json"
        document = json.loads(doc_path.read_text(encoding="utf-8"))
        document["sections"]["current_plan"]["entries"][0]["payload_ref"][
            "artifact_path"
        ] = "/outside-workspace/legacy/plan.json"
        doc_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        read = _run(self.tmp, "read", "--case-id", "case-demo")
        entry = read["sections"]["current_plan"]["entries"][0]
        self.assertIn("legacy absolute reference", entry["artifact_path"])
        self.assertNotIn("/outside-workspace", json.dumps(read, ensure_ascii=False))

    # --- path traversal hardening ---------------------------------------

    def test_path_like_ids_are_rejected_everywhere(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        bad_ids = ("../escape", "/etc/passwd", "a\\b", "..", str(self.tmp / "abs"))
        for bad in bad_ids:
            _run(
                self.tmp,
                "propose",
                "--case-id", bad,
                "--section", "current_plan",
                "--payload", str(payload),
                "--plan-id", "plan-a",
                "--base-revision", "0",
                "--change-reason", "路径穿越",
                expect_ok=False,
            )
            _run(self.tmp, "read", "--case-id", bad, expect_ok=False)
            _run(self.tmp, "confirm", "--proposal-id", bad, expect_ok=False)
            _run(self.tmp, "retry", "--outbox-id", bad, expect_ok=False)
            _run(
                self.tmp,
                "init-case",
                "--case-id", bad,
                "--issuer-id", "issuer-demo",
                "--listing-id", "0700.HK",
                expect_ok=False,
            )
        # Nothing escaped the workspace and nothing partial was written.
        self.assertEqual(
            [p.name for p in (self.workbench / "documents").iterdir()],
            ["case-demo.json"],
        )
        self.assertEqual(list((self.workbench / "proposals").iterdir()), [])
        self.assertEqual(list((self.workbench / "outbox").iterdir()), [])
        self.assertFalse((self.tmp / "escape.json").exists())
        manifest = json.loads(
            (self.workbench / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["cases"]), 1)

    def _rewrite_stored_artifact_path(self, proposal_id: str, new_path: str) -> None:
        proposal_path = self.workbench / "proposals" / f"{proposal_id}.json"
        stored = json.loads(proposal_path.read_text(encoding="utf-8"))
        stored["payload_ref"]["artifact_path"] = new_path
        target = Path(new_path)
        if target.is_file():
            stored["payload_ref"]["artifact_id"] = (
                "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
            )
        proposal_path.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _assert_confirm_left_no_trace(self, proposal_id: str) -> None:
        self.assertEqual(self._doc()["head_revision"], 0)
        self.assertEqual(self._doc()["sections"]["current_plan"]["entries"], [])
        self.assertEqual(self._proposal(proposal_id)["status"], "pending")
        self.assertEqual(
            [p.name for p in (self.workbench / "proposals").iterdir()],
            [f"{proposal_id}.json"],
        )
        self.assertEqual(list((self.workbench / "outbox").iterdir()), [])

    def test_legacy_absolute_path_outside_workspace_fails_closed(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        with tempfile.TemporaryDirectory() as outside:
            outside_payload = _write_payload(Path(outside))
            # artifact_id matches the outside file, so only the location
            # guard can fail the confirm.
            self._rewrite_stored_artifact_path(
                proposal["proposal_id"], str(outside_payload.resolve())
            )
            _run(
                self.tmp,
                "confirm",
                "--proposal-id", proposal["proposal_id"],
                expect_ok=False,
            )
            # Drive-unavailable branch must fail before touching the outbox.
            _run(
                self.tmp,
                "confirm",
                "--proposal-id", proposal["proposal_id"],
                "--drive-available", "false",
                expect_ok=False,
            )
        self._assert_confirm_left_no_trace(proposal["proposal_id"])

    def test_stored_relative_path_escaping_workspace_fails_closed(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        self._rewrite_stored_artifact_path(
            proposal["proposal_id"], "../../outside.json"
        )
        _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self._assert_confirm_left_no_trace(proposal["proposal_id"])

    def test_legacy_absolute_path_inside_workspace_migrates_on_retry(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        proposal = self._propose(payload, base=0)
        self._rewrite_stored_artifact_path(
            proposal["proposal_id"], str(payload.resolve())
        )
        queued = _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            "--drive-available", "false",
        )
        self.assertEqual(queued["status"], "pending_sync")
        # The queued outbox item already carries the migrated relative path.
        outbox_item = json.loads(
            (
                self.workbench / "outbox" / f"{proposal['proposal_id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(outbox_item["payload_ref"]["artifact_path"], "plan.json")
        self._assert_no_absolute_path(json.dumps(outbox_item, ensure_ascii=False))

        retried = _run(self.tmp, "retry", "--outbox-id", proposal["proposal_id"])
        self.assertEqual(retried["status"], "applied")
        entry = self._doc()["sections"]["current_plan"]["entries"][0]
        self.assertEqual(entry["payload_ref"]["artifact_path"], "plan.json")

    def test_read_masks_legacy_absolute_path_inside_workspace(self) -> None:
        self._init_case()
        payload = _write_payload(self.tmp)
        proposal = self._propose(payload, base=0)
        _run(self.tmp, "confirm", "--proposal-id", proposal["proposal_id"])
        doc_path = self.workbench / "documents" / "case-demo.json"
        document = json.loads(doc_path.read_text(encoding="utf-8"))
        document["sections"]["current_plan"]["entries"][0]["payload_ref"][
            "artifact_path"
        ] = str(payload.resolve())
        doc_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        read = _run(self.tmp, "read", "--case-id", "case-demo")
        entry = read["sections"]["current_plan"]["entries"][0]
        self.assertIn("legacy absolute reference", entry["artifact_path"])
        self._assert_no_absolute_path(json.dumps(read, ensure_ascii=False))


    # --- symlink hardening ----------------------------------------------

    def _assert_clean_failure(self, proc) -> None:
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(
            len(proc.stderr.strip().splitlines()),
            1,
            f"stderr must be a single line: {proc.stderr!r}",
        )

    def test_symlinked_workbench_root_is_refused(self) -> None:
        self.workbench.parent.mkdir(parents=True)
        target = self.tmp / "elsewhere"
        target.mkdir()
        self.workbench.symlink_to(target, target_is_directory=True)
        proc = _run(
            self.tmp,
            "init-case",
            "--case-id", "case-demo",
            "--issuer-id", "issuer-demo",
            "--listing-id", "0700.HK",
            expect_ok=False,
        )
        self._assert_clean_failure(proc)
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_documents_dir_is_refused(self) -> None:
        self.workbench.mkdir(parents=True)
        target = self.tmp / "outside-documents"
        target.mkdir()
        (self.workbench / "documents").symlink_to(target, target_is_directory=True)
        proc = _run(
            self.tmp,
            "init-case",
            "--case-id", "case-demo",
            "--issuer-id", "issuer-demo",
            "--listing-id", "0700.HK",
            expect_ok=False,
        )
        self._assert_clean_failure(proc)
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_proposals_and_outbox_dirs_are_refused(self) -> None:
        self._init_case()
        for subdir in ("proposals", "outbox"):
            with self.subTest(subdir=subdir):
                link = self.workbench / subdir
                link.rmdir()
                target = self.tmp / f"outside-{subdir}"
                target.mkdir()
                link.symlink_to(target, target_is_directory=True)
                proc = _run(
                    self.tmp, "read", "--case-id", "case-demo", expect_ok=False
                )
                self._assert_clean_failure(proc)
                self.assertEqual(list(target.iterdir()), [])
                link.unlink()
                link.mkdir()

    # --- resolve-conflict payload verification ---------------------------

    def test_conflict_new_plan_verifies_payload_ref(self) -> None:
        proposal_id = self._make_conflict("case-new-plan-verify")
        # Tamper with the proposed payload after the conflict was recorded.
        (self.tmp / "case-new-plan-verify-p2.json").write_text(
            json.dumps({"status": "watch", "tampered": True}), encoding="utf-8"
        )
        proc = _run(
            self.tmp,
            "resolve-conflict",
            "--proposal-id", proposal_id,
            "--choice", "new_plan",
            "--new-plan-id", "plan-b",
            expect_ok=False,
        )
        self._assert_clean_failure(proc)
        document = self._doc("case-new-plan-verify")
        self.assertEqual(document["head_revision"], 1)
        self.assertEqual(
            len(document["sections"]["current_plan"]["entries"]), 1
        )
        self.assertEqual(self._proposal(proposal_id)["status"], "conflict")

    # --- corrupted / tampered state files --------------------------------

    def test_corrupted_proposal_json_fails_closed(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        proposal_path = (
            self.workbench / "proposals" / f"{proposal['proposal_id']}.json"
        )
        proposal_path.write_text("{ not json", encoding="utf-8")
        proc = _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self._assert_clean_failure(proc)
        self.assertEqual(self._doc()["head_revision"], 0)
        self.assertEqual(
            list((self.workbench / "outbox").iterdir()), []
        )

    def test_tampered_proposal_fields_fail_closed(self) -> None:
        self._init_case()
        mutations = (
            ("schema_version", lambda p: p.update(schema_version=99)),
            ("proposal_id", lambda p: p.update(proposal_id="prop-other")),
            ("case_id", lambda p: p.pop("case_id")),
            ("document", lambda p: p.update(document="documents/other.json")),
            ("section", lambda p: p.update(section="not-a-section")),
            ("status", lambda p: p.update(status="weird")),
            ("base_revision", lambda p: p.update(base_revision="0")),
            (
                "payload_ref",
                lambda p: p.update(payload_ref={"artifact_path": "plan.json"}),
            ),
        )
        for index, (field, mutate) in enumerate(mutations):
            with self.subTest(field=field):
                proposal = self._propose(
                    _write_payload(self.tmp, f"tamper-{index}.json"), base=0
                )
                proposal_path = (
                    self.workbench / "proposals" / f"{proposal['proposal_id']}.json"
                )
                stored = json.loads(proposal_path.read_text(encoding="utf-8"))
                mutate(stored)
                proposal_path.write_text(
                    json.dumps(stored, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                proc = _run(
                    self.tmp,
                    "confirm",
                    "--proposal-id", proposal["proposal_id"],
                    expect_ok=False,
                )
                self._assert_clean_failure(proc)
                proposal_path.unlink()
        self.assertEqual(self._doc()["head_revision"], 0)

    def test_tampered_proposal_case_id_cannot_cross_write(self) -> None:
        # 篡改 proposal 的 case_id（c1 的 payload 写入 c2）必须 fail closed。
        self._init_case()
        self._init_case("case-other")
        proposal = self._propose(_write_payload(self.tmp), base=0)
        proposal_path = (
            self.workbench / "proposals" / f"{proposal['proposal_id']}.json"
        )
        stored = json.loads(proposal_path.read_text(encoding="utf-8"))
        stored["case_id"] = "case-other"
        stored["document"] = "documents/case-other.json"
        proposal_path.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        proc = _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self._assert_clean_failure(proc)
        self.assertIn("case_id", proc.stderr)
        # c2 文档未被写入，proposal 保持 pending。
        other = self._doc("case-other")
        self.assertEqual(other["head_revision"], 0)
        self.assertEqual(other["sections"]["current_plan"]["entries"], [])
        self.assertEqual(self._proposal(proposal["proposal_id"])["status"], "pending")

    def test_tampered_outbox_case_id_cannot_cross_write(self) -> None:
        self._init_case()
        self._init_case("case-other")
        proposal = self._propose(_write_payload(self.tmp), base=0)
        _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            "--drive-available", "false",
        )
        outbox_path = self.workbench / "outbox" / f"{proposal['proposal_id']}.json"
        stored = json.loads(outbox_path.read_text(encoding="utf-8"))
        stored["case_id"] = "case-other"
        stored["document"] = "documents/case-other.json"
        outbox_path.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        proc = _run(
            self.tmp,
            "retry",
            "--outbox-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self._assert_clean_failure(proc)
        self.assertIn("case_id", proc.stderr)
        other = self._doc("case-other")
        self.assertEqual(other["head_revision"], 0)
        self.assertEqual(other["sections"]["current_plan"]["entries"], [])

    def test_corrupted_or_tampered_outbox_item_fails_closed(self) -> None:
        self._init_case()
        proposal = self._propose(_write_payload(self.tmp), base=0)
        _run(
            self.tmp,
            "confirm",
            "--proposal-id", proposal["proposal_id"],
            "--drive-available", "false",
        )
        outbox_path = self.workbench / "outbox" / f"{proposal['proposal_id']}.json"

        outbox_path.write_text('{"outbox_id": "x"', encoding="utf-8")
        proc = _run(
            self.tmp,
            "retry",
            "--outbox-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self._assert_clean_failure(proc)

        queued = self._proposal(proposal["proposal_id"])
        queued["outbox_id"] = "someone-else"
        outbox_path.write_text(
            json.dumps(queued, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        proc = _run(
            self.tmp,
            "retry",
            "--outbox-id", proposal["proposal_id"],
            expect_ok=False,
        )
        self._assert_clean_failure(proc)
        self.assertEqual(self._doc()["head_revision"], 0)


if __name__ == "__main__":
    unittest.main()
