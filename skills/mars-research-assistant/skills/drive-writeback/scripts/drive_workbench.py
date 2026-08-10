#!/usr/bin/env python3
"""Drive 投研工作台本地合同（Batch 7）：纯本地模拟，绝不访问真实 Google Drive。

工作区状态目录 ``<workspace>/mars-research/drive-workbench/``（全部用户可见）：

- ``manifest.json``：案例清单；
- ``documents/<case_id>.json``：一个案例一个主文档，四区 append-only；
- ``proposals/``：写入提议（pending）、冲突摘要与回执；
- ``outbox/``：Drive 暂不可用时已确认但待同步（pending_sync）的条目。

纪律与 drive-writeback SKILL 一致：提议与确认分离、确认必须回指 proposal ID、
父 revision 不匹配时显式冲突并要求用户选择、绝不静默覆盖、``user_sections``
为本 skill 永不改写的自由内容。完整研究 JSON/Markdown 不复制进主文档，只存
artifact 引用（不超过 500 字符的结构性摘要）。引用以可移植形式持久化：相对
工作区根的路径 + sha256 派生的稳定 ``artifact_id``（兼作完整性校验值），绝不
写入本机绝对路径；confirm/retry 在本机按工作区根解析回实际文件核对存在性与
sha256。旧格式的绝对路径引用仅当解析在工作区内时才于核对时迁移为相对形式；
解析到工作区外一律 fail closed，confirm/retry 不再写入任何文档或 outbox。
read 输出永不回显绝对路径。进入文件路径的 ID（case_id / proposal_id /
outbox_id）一律拒绝路径形态输入（绝对路径、``..``、``/``、``\\``），并在
读写前解析最终路径、校验其仍落在目标子目录内。工作区状态路径
（mars-research / drive-workbench / documents / proposals / outbox 及其父目录）
若为符号链接一律拒绝操作，读写绝不借符号链接越出工作台根（fail closed）。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid


class WorkbenchError(ValueError):
    """Fail closed instead of silently writing or overwriting workbench state."""


ENGINE = "skills/drive-writeback/scripts/drive_workbench.py"
ENGINE_VERSION = "1.0.0"
SCHEMA_VERSION = 1
SECTIONS = ("idea_log", "current_plan", "decision_log", "review_log")
CONFLICT_OPTIONS = ("keep_local", "keep_incoming", "merge", "new_plan")
SUMMARY_LIMIT = 500
WORKBENCH_REL = Path("mars-research") / "drive-workbench"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchError(f"{context} requires text")
    return value.strip()


def _is_schema_one(value: object) -> bool:
    """Strict schema check: JSON true must not pass as integer 1."""
    return type(value) is int and value == SCHEMA_VERSION


def _safe_id(value: object, context: str) -> str:
    """将进入文件名的 ID 限制为纯标识符：拒绝任何路径形态输入（fail closed）。"""
    text = _text(value, context)
    if (
        Path(text).is_absolute()
        or "/" in text
        or "\\" in text
        or ".." in text
        or Path(text).name != text
    ):
        raise WorkbenchError(f"{context} must be a plain identifier, not a path")
    return text


def _inside(directory: Path, filename: str) -> Path:
    """解析最终路径并校验其仍落在目标子目录内，否则 fail closed。"""
    base = directory.resolve()
    path = (base / filename).resolve()
    if path.parent != base:
        raise WorkbenchError(f"path escapes workspace directory: {filename}")
    return path


def _workbench(workspace: str) -> Path:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise WorkbenchError(f"workspace is not a directory: {workspace}")
    workbench = root / WORKBENCH_REL
    # Fail closed on symlinked state paths: writing through them would escape
    # the workbench root even though the lexical path looks contained.
    for label, path in (
        ("workspace state directory", workbench.parent),
        ("workbench root", workbench),
        ("documents directory", workbench / "documents"),
        ("proposals directory", workbench / "proposals"),
        ("outbox directory", workbench / "outbox"),
    ):
        if path.is_symlink():
            raise WorkbenchError(f"{label} must not be a symlink (fail closed)")
    try:
        workbench.resolve().relative_to(root)
    except ValueError as error:
        raise WorkbenchError(
            "workbench root resolves outside the workspace (fail closed)"
        ) from error
    return workbench


def _workspace_root(workbench: Path) -> Path:
    """Resolved workspace root that portable artifact paths are relative to."""
    return workbench.parent.parent


def _load_json(path: Path, context: str) -> dict:
    if path.is_symlink():
        raise WorkbenchError(f"{context} must not be a symlink: {path}")
    if not path.is_file():
        raise WorkbenchError(f"{context} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkbenchError(f"{context} is not readable JSON: {path}") from error
    if not isinstance(data, dict):
        raise WorkbenchError(f"{context} must be a JSON object: {path}")
    return data


def _write_json_new(path: Path, payload: dict) -> None:
    """Exclusively create a new artifact file; never overwrite."""
    try:
        with open(path, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError as error:
        raise WorkbenchError(f"File exists (refuse to overwrite): {path}") from error


def _save_json(path: Path, payload: dict) -> None:
    """Rewrite mutable workbench state (manifest / document / proposal status)."""
    if path.is_symlink():
        raise WorkbenchError(f"refuse to write through a symlink: {path}")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _load_manifest(workbench: Path) -> dict:
    manifest_path = workbench / "manifest.json"
    if not manifest_path.is_file():
        return {"schema_version": SCHEMA_VERSION, "cases": []}
    manifest = _load_json(manifest_path, "manifest")
    if not _is_schema_one(manifest.get("schema_version")):
        raise WorkbenchError("manifest schema_version mismatch")
    if not isinstance(manifest.get("cases"), list):
        raise WorkbenchError("manifest cases must be a list")
    case_ids: set[str] = set()
    for case in manifest["cases"]:
        if not isinstance(case, dict):
            raise WorkbenchError("manifest case must be an object")
        case_id = _safe_id(case.get("case_id"), "manifest case_id")
        if case_id in case_ids:
            raise WorkbenchError("manifest case_id values must be unique")
        case_ids.add(case_id)
        _text(case.get("issuer_id"), "manifest issuer_id")
        listing_ids = case.get("listing_ids")
        if (
            not isinstance(listing_ids, list)
            or not listing_ids
            or any(not isinstance(item, str) or not item.strip() for item in listing_ids)
            or len(set(listing_ids)) != len(listing_ids)
        ):
            raise WorkbenchError("manifest listing_ids must be a non-empty unique list")
        if case.get("doc") != f"documents/{case['case_id']}.json":
            raise WorkbenchError("manifest document mismatch")
    return manifest


def _find_case(manifest: dict, case_id: str) -> dict | None:
    for case in manifest["cases"]:
        if case.get("case_id") == case_id:
            return case
    return None


def _doc_path(workbench: Path, case_id: str) -> Path:
    return _inside(workbench / "documents", f"{_safe_id(case_id, 'case-id')}.json")


def _proposal_path(workbench: Path, proposal_id: str) -> Path:
    return _inside(
        workbench / "proposals", f"{_safe_id(proposal_id, 'proposal-id')}.json"
    )


def _outbox_path(workbench: Path, outbox_id: str) -> Path:
    return _inside(workbench / "outbox", f"{_safe_id(outbox_id, 'outbox-id')}.json")


def _load_document(workbench: Path, case_id: str) -> dict:
    manifest = _load_manifest(workbench)
    manifest_case = _find_case(manifest, case_id)
    if manifest_case is None:
        raise WorkbenchError(f"unknown case_id (run init-case first): {case_id}")
    document = _load_json(_doc_path(workbench, case_id), "document")
    if not _is_schema_one(document.get("schema_version")):
        raise WorkbenchError("document schema_version mismatch")
    if document.get("case_id") != case_id:
        raise WorkbenchError("document case_id mismatch")
    if document.get("issuer_id") != manifest_case["issuer_id"]:
        raise WorkbenchError("document issuer_id does not match manifest")
    if document.get("listing_ids") != manifest_case["listing_ids"]:
        raise WorkbenchError("document listing_ids do not match manifest")
    for section in SECTIONS:
        entries = document.get("sections", {}).get(section, {}).get("entries")
        if not isinstance(entries, list):
            raise WorkbenchError(f"document section corrupted: {section}")
    return document


def _make_payload_ref(payload_arg: str, workspace_root: Path) -> dict:
    """Build a portable artifact reference; never embed the full research payload.

    The persisted reference is a workspace-relative path plus a stable
    sha256-derived artifact_id (which doubles as the integrity checksum);
    host-absolute paths are never stored.
    """
    payload_path = Path(payload_arg)
    if not payload_path.is_file():
        raise WorkbenchError(f"payload artifact not found: {payload_arg}")
    try:
        raw = payload_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkbenchError(f"payload must be an existing JSON file: {payload_arg}") from error
    if not isinstance(payload, dict):
        raise WorkbenchError("payload must be a JSON object")
    resolved = payload_path.resolve()
    try:
        rel_path = resolved.relative_to(workspace_root)
    except ValueError as error:
        raise WorkbenchError(
            "payload artifact must live inside the workspace so its reference "
            f"stays portable: {payload_arg}"
        ) from error
    digest = hashlib.sha256(raw).hexdigest()
    summary_source = {
        "identity": payload.get("identity"),
        "status": payload.get("status"),
        "top_level_keys": sorted(payload.keys())[:20],
        "payload_sha256": digest,
    }
    summary = json.dumps(summary_source, ensure_ascii=False, separators=(",", ":"))
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[: SUMMARY_LIMIT - 1] + "…"
    return {
        "artifact_path": rel_path.as_posix(),
        "artifact_id": f"sha256:{digest}",
        "summary": summary,
    }


def _verify_payload_ref(ref: dict, workspace_root: Path) -> None:
    """Confirm-time guard: payload unchanged and not embedded into the document.

    References are workspace-relative and are resolved against the workspace
    root with strict containment. A legacy absolute path is migrated to the
    relative form only when it resolves inside the workspace; a path that
    resolves outside fails closed before any document/outbox write happens.
    """
    stored = ref.get("artifact_path", "")
    if not isinstance(stored, str) or not stored:
        raise WorkbenchError("payload_ref artifact_path missing")
    path = Path(stored)
    if path.is_symlink():
        raise WorkbenchError("payload artifact must not be a symlink")
    if not path.is_absolute():
        path = workspace_root / path
    resolved = path.resolve()
    try:
        rel_path = resolved.relative_to(workspace_root)
    except ValueError as error:
        raise WorkbenchError(
            "artifact_path resolves outside the workspace; refusing to confirm: "
            f"{_display_artifact_path(stored)}"
        ) from error
    if not resolved.is_file():
        raise WorkbenchError(f"payload artifact missing at confirm time: {stored}")
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if ref.get("artifact_id") != f"sha256:{digest}":
        raise WorkbenchError(
            "payload changed after proposal; propose again from the new artifact"
        )
    if ref.get("artifact_path") != rel_path.as_posix():
        # Verified reference (legacy absolute or unnormalized relative) inside
        # the workspace: migrate it in place so everything persisted from now
        # on (document, receipt, outbox) is portable.
        ref["artifact_path"] = rel_path.as_posix()
    summary = ref.get("summary")
    if not isinstance(summary, str) or len(summary) > SUMMARY_LIMIT:
        raise WorkbenchError("payload_ref summary exceeds the 500-character limit")
    if raw.decode("utf-8") in json.dumps(ref, ensure_ascii=False):
        raise WorkbenchError("payload content must not be embedded into the document")


def _new_entry(
    *,
    plan_id: str,
    revision: int,
    parent_revision: int | None,
    trigger_evidence: object,
    change_reason: str,
    payload_ref: dict,
) -> dict:
    return {
        "plan_id": plan_id,
        "revision": revision,
        "parent_revision": parent_revision,
        "trigger_evidence": trigger_evidence,
        "change_reason": change_reason,
        "as_of": _now(),
        "payload_ref": payload_ref,
    }


_UNSET = object()


def _apply_entry(
    workbench: Path,
    proposal: dict,
    *,
    plan_id: str | None = None,
    revision: int | None = None,
    parent_revision: object = _UNSET,
    payload_ref: dict | None = None,
    change_reason: str | None = None,
) -> dict:
    """Append the proposal entry, bump head_revision, mark applied, write receipt."""
    document = _load_document(workbench, proposal["case_id"])
    head = document["head_revision"]
    entry = _new_entry(
        plan_id=plan_id or proposal["plan_id"],
        revision=revision if revision is not None else head + 1,
        parent_revision=head if parent_revision is _UNSET else parent_revision,
        trigger_evidence=proposal.get("trigger_evidence"),
        change_reason=change_reason or proposal["change_reason"],
        payload_ref=payload_ref or proposal["payload_ref"],
    )
    section = proposal["section"]
    document["sections"][section]["entries"].append(entry)
    document["head_revision"] = head + 1
    document["updated_as_of"] = _now()
    _save_json(_doc_path(workbench, proposal["case_id"]), document)

    proposal["status"] = "applied"
    proposal["resolved_as_of"] = _now()
    _save_json(_proposal_path(workbench, proposal["proposal_id"]), proposal)

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "receipt_id": f"{proposal['proposal_id']}-receipt",
        "proposal_id": proposal["proposal_id"],
        "case_id": proposal["case_id"],
        "document": f"documents/{proposal['case_id']}.json",
        "section": section,
        "plan_id": entry["plan_id"],
        "revision": entry["revision"],
        "parent_revision": entry["parent_revision"],
        "head_revision": document["head_revision"],
        "payload_ref": entry["payload_ref"],
        "applied_as_of": entry["as_of"],
    }
    _write_json_new(
        _proposal_path(workbench, f"{proposal['proposal_id']}-receipt"), receipt
    )
    return {"entry": entry, "head_revision": document["head_revision"], "receipt": receipt}


def _enter_conflict(workbench: Path, proposal: dict, head: int) -> dict:
    """Record an explicit conflict; never overwrite the local head silently."""
    document = _load_document(workbench, proposal["case_id"])
    section_entries = document["sections"][proposal["section"]]["entries"]
    local_head_entry = section_entries[-1] if section_entries else None
    proposed_entry = {
        "plan_id": proposal["plan_id"],
        "revision": proposal["base_revision"] + 1,
        "parent_revision": proposal["base_revision"],
        "trigger_evidence": proposal.get("trigger_evidence"),
        "change_reason": proposal["change_reason"],
        "payload_ref": proposal["payload_ref"],
    }
    conflict = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "proposal_id": proposal["proposal_id"],
        "case_id": proposal["case_id"],
        "section": proposal["section"],
        "base_revision": proposal["base_revision"],
        "head_revision": head,
        "local_head_entry": local_head_entry,
        "proposed_entry": proposed_entry,
        "options": list(CONFLICT_OPTIONS),
        "detected_as_of": _now(),
        "status": "awaiting_user_choice",
    }
    conflict_rel = f"proposals/{proposal['proposal_id']}-conflict.json"
    conflict_path = _proposal_path(workbench, f"{proposal['proposal_id']}-conflict")
    if not conflict_path.is_file():
        _write_json_new(conflict_path, conflict)
    proposal["status"] = "conflict"
    proposal["resolved_as_of"] = None
    _save_json(_proposal_path(workbench, proposal["proposal_id"]), proposal)
    return {
        "status": "conflict",
        "proposal_id": proposal["proposal_id"],
        "conflict_file": conflict_rel,
        "base_revision": proposal["base_revision"],
        "head_revision": head,
        "options": list(CONFLICT_OPTIONS),
    }


def _payload_identity(ref: dict, workspace_root: Path) -> dict[str, object]:
    """Read the referenced payload identity (fail closed).

    Uses the same resolution rules as _verify_payload_ref: workspace-relative
    paths resolve against the workspace root, a legacy absolute path is only
    accepted when it resolves inside the workspace, anything else fails closed.
    """
    stored = ref.get("artifact_path", "")
    if not isinstance(stored, str) or not stored:
        raise WorkbenchError("payload_ref artifact_path missing")
    path = Path(stored)
    if path.is_symlink():
        raise WorkbenchError("payload artifact must not be a symlink")
    if not path.is_absolute():
        path = workspace_root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as error:
        raise WorkbenchError(
            "artifact_path resolves outside the workspace; refusing to load: "
            f"{_display_artifact_path(stored)}"
        ) from error
    if not resolved.is_file():
        raise WorkbenchError("payload artifact missing when loading proposal")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkbenchError("payload artifact is not readable JSON") from error
    if not isinstance(payload, dict):
        raise WorkbenchError("payload artifact must be a JSON object")
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        raise WorkbenchError("payload artifact identity missing")
    case_id = _text(identity.get("case_id"), "payload artifact identity case_id")
    issuer_id = _text(identity.get("issuer_id"), "payload artifact identity issuer_id")
    raw_listing_ids = identity.get("listing_ids")
    if raw_listing_ids is None:
        listing_ids = [
            _text(identity.get("listing_id"), "payload artifact identity listing_id")
        ]
    else:
        if not isinstance(raw_listing_ids, list) or not raw_listing_ids:
            raise WorkbenchError(
                "payload artifact identity listing_ids must be a non-empty list"
            )
        listing_ids = [
            _text(item, "payload artifact identity listing_id")
            for item in raw_listing_ids
        ]
    if len(set(listing_ids)) != len(listing_ids):
        raise WorkbenchError("payload artifact identity listing_ids must be unique")
    return {"case_id": case_id, "issuer_id": issuer_id, "listing_ids": listing_ids}


def _payload_case_id(ref: dict, workspace_root: Path) -> str:
    """Compatibility wrapper returning the referenced payload case_id."""
    return str(_payload_identity(ref, workspace_root)["case_id"])


def _verify_case_binding(workbench: Path, data: dict, context: str) -> None:
    """Bind payload case, issuer and listing IDs to the manifest case."""
    manifest = _load_manifest(workbench)
    case_id = _safe_id(data.get("case_id"), f"{context} case_id")
    manifest_case = _find_case(manifest, case_id)
    if manifest_case is None:
        raise WorkbenchError(f"{context} references unknown case_id (fail closed)")
    payload_identity = _payload_identity(
        data["payload_ref"], _workspace_root(workbench)
    )
    if payload_identity["case_id"] != case_id:
        raise WorkbenchError(
            f"{context} case_id does not match payload identity case_id "
            "(fail closed)"
        )
    if payload_identity["issuer_id"] != manifest_case["issuer_id"]:
        raise WorkbenchError(
            f"{context} issuer_id does not match manifest case issuer_id (fail closed)"
        )
    if not set(payload_identity["listing_ids"]).issubset(
        set(manifest_case["listing_ids"])
    ):
        raise WorkbenchError(
            f"{context} listing_id does not match manifest case listing_ids (fail closed)"
        )


_PROPOSAL_STATUSES = ("pending", "applied", "conflict", "abandoned", "pending_sync")


def _validate_proposal(data: dict, proposal_id: object, context: str) -> dict:
    """Fail closed on any tampered, corrupted, or schema-mismatched record."""
    if not isinstance(proposal_id, str) or not proposal_id:
        raise WorkbenchError(f"{context} proposal_id missing")
    if not _is_schema_one(data.get("schema_version")):
        raise WorkbenchError(f"{context} schema_version mismatch: {proposal_id}")
    if data.get("proposal_id") != proposal_id:
        raise WorkbenchError(f"{context} proposal_id mismatch: {proposal_id}")
    case_id = _safe_id(data.get("case_id"), f"{context} case_id")
    if data.get("document") != f"documents/{case_id}.json":
        raise WorkbenchError(f"{context} document mismatch: {proposal_id}")
    if data.get("section") not in SECTIONS:
        raise WorkbenchError(f"{context} section invalid: {proposal_id}")
    if data.get("status") not in _PROPOSAL_STATUSES:
        raise WorkbenchError(f"{context} status invalid: {proposal_id}")
    base_revision = data.get("base_revision")
    if (
        not isinstance(base_revision, int)
        or isinstance(base_revision, bool)
        or base_revision < 0
    ):
        raise WorkbenchError(f"{context} base_revision invalid: {proposal_id}")
    payload_ref = data.get("payload_ref")
    if not isinstance(payload_ref, dict) or not all(
        isinstance(payload_ref.get(key), str)
        for key in ("artifact_path", "artifact_id", "summary")
    ):
        raise WorkbenchError(f"{context} payload_ref invalid: {proposal_id}")
    return data


def _load_proposal(workbench: Path, proposal_id: str) -> dict:
    proposal_id = _safe_id(proposal_id, "proposal-id")
    data = _load_json(_proposal_path(workbench, proposal_id), "proposal")
    proposal = _validate_proposal(data, proposal_id, "proposal")
    _verify_case_binding(workbench, proposal, "proposal")
    return proposal


def _load_outbox_item(workbench: Path, outbox_id: str) -> dict:
    data = _load_json(_outbox_path(workbench, outbox_id), "outbox item")
    if data.get("outbox_id") != outbox_id:
        raise WorkbenchError(f"outbox item id mismatch: {outbox_id}")
    if data.get("proposal_id") != outbox_id:
        raise WorkbenchError(f"outbox item proposal_id mismatch: {outbox_id}")
    item = _validate_proposal(data, data.get("proposal_id"), "outbox item")
    _verify_case_binding(workbench, item, "outbox item")
    return item


def cmd_init_case(args: argparse.Namespace) -> dict:
    workbench = _workbench(args.workspace)
    case_id = _safe_id(args.case_id, "case-id")
    issuer_id = _text(args.issuer_id, "issuer-id")
    listing_ids = [_text(item, "listing-id") for item in args.listing_id]
    if not listing_ids or len(set(listing_ids)) != len(listing_ids):
        raise WorkbenchError("listing-id values must be non-empty and unique")
    for subdir in ("documents", "proposals", "outbox"):
        (workbench / subdir).mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest(workbench)
    existing = _find_case(manifest, case_id)
    if existing is not None:
        return {
            "status": "existing",
            "case_id": case_id,
            "document": existing["doc"],
            "note": "case already initialized; nothing overwritten",
        }

    created_as_of = _now()
    document = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "case_id": case_id,
        "issuer_id": issuer_id,
        "listing_ids": listing_ids,
        "created_as_of": created_as_of,
        "updated_as_of": created_as_of,
        "sections": {section: {"entries": []} for section in SECTIONS},
        "user_sections": {},
        "head_revision": 0,
    }
    _write_json_new(_doc_path(workbench, case_id), document)
    manifest["cases"].append(
        {
            "case_id": case_id,
            "issuer_id": issuer_id,
            "listing_ids": listing_ids,
            "doc": f"documents/{case_id}.json",
            "created_as_of": created_as_of,
        }
    )
    _save_json(workbench / "manifest.json", manifest)
    return {
        "status": "created",
        "case_id": case_id,
        "issuer_id": issuer_id,
        "listing_ids": listing_ids,
        "document": f"documents/{case_id}.json",
        "manifest": "manifest.json",
    }


def cmd_propose(args: argparse.Namespace) -> dict:
    workbench = _workbench(args.workspace)
    case_id = _safe_id(args.case_id, "case-id")
    section = _text(args.section, "section")
    if section not in SECTIONS:
        raise WorkbenchError(f"section must be one of {list(SECTIONS)}")
    plan_id = _text(args.plan_id, "plan-id")
    change_reason = _text(args.change_reason, "change-reason")
    _load_document(workbench, case_id)  # case must be initialized first
    payload_ref = _make_payload_ref(args.payload, _workspace_root(workbench))
    _verify_case_binding(
        workbench,
        {"case_id": case_id, "payload_ref": payload_ref},
        "proposal",
    )

    proposal_id = f"prop-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "proposal_id": proposal_id,
        "status": "pending",
        "case_id": case_id,
        "document": f"documents/{case_id}.json",
        "section": section,
        "plan_id": plan_id,
        "base_revision": args.base_revision,
        "trigger_evidence": args.trigger_evidence,
        "change_reason": change_reason,
        "payload_ref": payload_ref,
        "created_as_of": _now(),
        "resolved_as_of": None,
    }
    _write_json_new(_proposal_path(workbench, proposal_id), proposal)
    return {
        "status": "pending",
        "proposal_id": proposal_id,
        "document": proposal["document"],
        "section": section,
        "plan_id": plan_id,
        "base_revision": args.base_revision,
        "change_reason": change_reason,
        "payload_ref": payload_ref,
        "confirmation": "required: confirm must reference this proposal_id",
    }


def cmd_confirm(args: argparse.Namespace) -> dict:
    workbench = _workbench(args.workspace)
    proposal = _load_proposal(workbench, args.proposal_id)
    if proposal.get("status") != "pending":
        raise WorkbenchError(
            f"proposal already consumed (status={proposal.get('status')}); "
            "confirmation must reference a pending proposal_id"
        )
    document = _load_document(workbench, proposal["case_id"])
    head = document["head_revision"]
    if proposal["base_revision"] != head:
        return _enter_conflict(workbench, proposal, head)

    _verify_payload_ref(proposal["payload_ref"], _workspace_root(workbench))
    if args.drive_available == "false":
        proposal["status"] = "pending_sync"
        proposal["queued_as_of"] = _now()
        _save_json(_proposal_path(workbench, proposal["proposal_id"]), proposal)
        outbox_item = dict(proposal)
        outbox_item["outbox_id"] = proposal["proposal_id"]
        _write_json_new(
            _outbox_path(workbench, proposal["proposal_id"]), outbox_item
        )
        return {
            "status": "pending_sync",
            "proposal_id": proposal["proposal_id"],
            "outbox_id": proposal["proposal_id"],
            "outbox": f"outbox/{proposal['proposal_id']}.json",
            "note": "Drive unavailable; queued visibly, no background upload, "
            "parent revision is re-checked on retry",
        }

    applied = _apply_entry(workbench, proposal)
    return {
        "status": "applied",
        "proposal_id": proposal["proposal_id"],
        "document": f"documents/{proposal['case_id']}.json",
        "section": proposal["section"],
        "plan_id": applied["entry"]["plan_id"],
        "revision": applied["entry"]["revision"],
        "parent_revision": applied["entry"]["parent_revision"],
        "head_revision": applied["head_revision"],
        "receipt": f"proposals/{proposal['proposal_id']}-receipt.json",
    }


def cmd_resolve_conflict(args: argparse.Namespace) -> dict:
    workbench = _workbench(args.workspace)
    proposal = _load_proposal(workbench, args.proposal_id)
    if proposal.get("status") != "conflict":
        raise WorkbenchError(
            f"proposal is not in conflict (status={proposal.get('status')})"
        )
    choice = args.choice
    if choice == "keep_local":
        proposal["status"] = "abandoned"
        proposal["resolved_as_of"] = _now()
        _save_json(_proposal_path(workbench, proposal["proposal_id"]), proposal)
        return {
            "status": "abandoned",
            "choice": "keep_local",
            "proposal_id": proposal["proposal_id"],
            "note": "local head kept; incoming proposal discarded, nothing written",
        }
    if choice == "new_plan":
        new_plan_id = _text(args.new_plan_id or "", "new-plan-id")
        _verify_payload_ref(proposal["payload_ref"], _workspace_root(workbench))
        applied = _apply_entry(
            workbench,
            proposal,
            plan_id=new_plan_id,
            revision=1,
            parent_revision=None,
        )
    elif choice == "merge":
        if not args.payload:
            raise WorkbenchError(
                "merge requires a manually merged payload; re-run with "
                "--payload <merged.json> (fail closed, no auto-merge)"
            )
        merged_ref = _make_payload_ref(args.payload, _workspace_root(workbench))
        _verify_payload_ref(merged_ref, _workspace_root(workbench))
        _verify_case_binding(
            workbench,
            {"case_id": proposal["case_id"], "payload_ref": merged_ref},
            "merged payload",
        )
        applied = _apply_entry(
            workbench,
            proposal,
            payload_ref=merged_ref,
            change_reason=f"merged: {proposal['change_reason']}",
        )
    else:  # keep_incoming
        _verify_payload_ref(proposal["payload_ref"], _workspace_root(workbench))
        applied = _apply_entry(workbench, proposal)
    return {
        "status": "applied",
        "choice": choice,
        "proposal_id": proposal["proposal_id"],
        "document": f"documents/{proposal['case_id']}.json",
        "section": proposal["section"],
        "plan_id": applied["entry"]["plan_id"],
        "revision": applied["entry"]["revision"],
        "parent_revision": applied["entry"]["parent_revision"],
        "head_revision": applied["head_revision"],
        "receipt": f"proposals/{proposal['proposal_id']}-receipt.json",
    }


def cmd_retry(args: argparse.Namespace) -> dict:
    workbench = _workbench(args.workspace)
    outbox_id = _safe_id(args.outbox_id, "outbox-id")
    outbox_path = _outbox_path(workbench, outbox_id)
    outbox_item = _load_outbox_item(workbench, outbox_id)
    if outbox_item.get("status") != "pending_sync":
        raise WorkbenchError(
            f"outbox item is not pending_sync (status={outbox_item.get('status')})"
        )
    if args.drive_available == "false":
        return {
            "status": "pending_sync",
            "outbox_id": outbox_id,
            "note": "Drive still unavailable; item stays in outbox, no background upload",
        }

    proposal = _load_proposal(workbench, outbox_item["proposal_id"])
    document = _load_document(workbench, proposal["case_id"])
    head = document["head_revision"]
    if proposal["base_revision"] != head:
        # Parent moved while queued: hand over to the explicit conflict flow.
        proposal["status"] = "pending"  # re-enter conflict detection cleanly
        outbox_path.unlink()
        return _enter_conflict(workbench, proposal, head)

    _verify_payload_ref(proposal["payload_ref"], _workspace_root(workbench))
    proposal["status"] = "pending"
    applied = _apply_entry(workbench, proposal)
    outbox_path.unlink()
    return {
        "status": "applied",
        "proposal_id": proposal["proposal_id"],
        "outbox_id": outbox_id,
        "outbox_removed": True,
        "document": f"documents/{proposal['case_id']}.json",
        "section": proposal["section"],
        "plan_id": applied["entry"]["plan_id"],
        "revision": applied["entry"]["revision"],
        "parent_revision": applied["entry"]["parent_revision"],
        "head_revision": applied["head_revision"],
        "receipt": f"proposals/{proposal['proposal_id']}-receipt.json",
    }


def _display_artifact_path(stored: object) -> str:
    """Display form for read output; never echoes a legacy absolute path."""
    if not isinstance(stored, str) or not stored:
        return "(missing artifact reference)"
    if Path(stored).is_absolute():
        return f"{Path(stored).name} (legacy absolute reference; re-propose to port)"
    return stored


def cmd_read(args: argparse.Namespace) -> dict:
    workbench = _workbench(args.workspace)
    case_id = _safe_id(args.case_id, "case-id")
    document = _load_document(workbench, case_id)
    sections = {}
    for section in SECTIONS:
        entries = document["sections"][section]["entries"]
        sections[section] = {
            "entry_count": len(entries),
            "entries": [
                {
                    "plan_id": entry["plan_id"],
                    "revision": entry["revision"],
                    "parent_revision": entry["parent_revision"],
                    "change_reason": entry["change_reason"],
                    "as_of": entry["as_of"],
                    "artifact_id": entry["payload_ref"]["artifact_id"],
                    "artifact_path": _display_artifact_path(
                        entry["payload_ref"]["artifact_path"]
                    ),
                }
                for entry in entries
            ],
        }
    user_sections = document.get("user_sections", {})
    return {
        "status": "ok",
        "case_id": case_id,
        "issuer_id": document["issuer_id"],
        "listing_ids": document["listing_ids"],
        "document": f"documents/{case_id}.json",
        "head_revision": document["head_revision"],
        "sections": sections,
        "user_sections_preserved": True,
        "user_section_keys": sorted(user_sections.keys())
        if isinstance(user_sections, dict)
        else [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Drive workbench local contract (no real Google Drive access)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_workspace(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--workspace", default=".", help="workspace root (default: cwd)")

    init_case = subparsers.add_parser("init-case", help="create a case and its document")
    init_case.add_argument("--case-id", required=True)
    init_case.add_argument("--issuer-id", required=True)
    init_case.add_argument("--listing-id", action="append", required=True)
    add_workspace(init_case)
    init_case.set_defaults(handler=cmd_init_case)

    propose = subparsers.add_parser("propose", help="write a pending proposal")
    propose.add_argument("--case-id", required=True)
    propose.add_argument("--section", required=True, choices=SECTIONS)
    propose.add_argument("--payload", required=True, help="existing artifact JSON path")
    propose.add_argument("--plan-id", required=True)
    propose.add_argument("--base-revision", required=True, type=int)
    propose.add_argument("--change-reason", required=True)
    propose.add_argument("--trigger-evidence", default=None)
    add_workspace(propose)
    propose.set_defaults(handler=cmd_propose)

    confirm = subparsers.add_parser("confirm", help="apply a pending proposal")
    confirm.add_argument("--proposal-id", required=True)
    confirm.add_argument("--drive-available", choices=("true", "false"), default="true")
    add_workspace(confirm)
    confirm.set_defaults(handler=cmd_confirm)

    resolve = subparsers.add_parser("resolve-conflict", help="resolve a conflict")
    resolve.add_argument("--proposal-id", required=True)
    resolve.add_argument("--choice", required=True, choices=CONFLICT_OPTIONS)
    resolve.add_argument("--new-plan-id", default=None)
    resolve.add_argument("--payload", default=None, help="merged payload for choice=merge")
    add_workspace(resolve)
    resolve.set_defaults(handler=cmd_resolve_conflict)

    retry = subparsers.add_parser("retry", help="retry an outbox item")
    retry.add_argument("--outbox-id", required=True)
    retry.add_argument("--drive-available", choices=("true", "false"), default="true")
    add_workspace(retry)
    retry.set_defaults(handler=cmd_retry)

    read = subparsers.add_parser("read", help="read the four-section summary")
    read.add_argument("--case-id", required=True)
    add_workspace(read)
    read.set_defaults(handler=cmd_read)
    return parser


def _one_line(error: BaseException) -> str:
    return str(error).replace("\r", " ").replace("\n", " ")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "base_revision", 0) is not None and getattr(args, "base_revision", 0) < 0:
        print("base-revision must be >= 0", file=sys.stderr)
        return 1
    try:
        result = args.handler(args)
    except WorkbenchError as error:
        print(f"drive_workbench failed: {_one_line(error)}", file=sys.stderr)
        return 1
    except Exception as error:
        # Corrupted state must surface as one line, never a traceback.
        print(
            f"drive_workbench failed: unexpected {type(error).__name__}: "
            f"{_one_line(error)}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
