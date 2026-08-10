#!/usr/bin/env python3
"""Render an investment-analysis discussion card (Markdown + optional JSON)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


class DiscussionCardError(ValueError):
    """Reject incomplete or out-of-scope inputs rather than inventing analysis."""


ENGINE = "skills/investment-analysis/scripts/render_discussion_card.py"
ENGINE_VERSION = "1.0.0"
CARD_VERSION = "v1.0.3-card-1"
SOURCE_KINDS = {
    "sec_filing",
    "regulatory_filing",
    "issuer_ir",
    "exchange",
    "issuer_announcement",
    "credible_media",
    "public_quote",
}
THESIS_STATUS = {"strengthened", "unchanged", "weakened", "falsified"}
THESIS_STATUS_LABELS = {
    "strengthened": "强化",
    "unchanged": "维持",
    "weakened": "弱化",
    "falsified": "证伪",
}
CONFIDENCE = {"high", "medium", "low"}
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
A_SHARE_SUFFIXES = (".SS", ".SH", ".SZ")
HK_SUFFIX = ".HK"
NO_INCREMENT_ACTION = "维持原方案/暂无动作"
TRADE_DIRECTIVE = re.compile(
    r"买入|卖出|增持|减持|加仓|减仓|建仓|平仓|下单|持仓比例|做空|沽空|卖空|"
    r"\bbuy\b|\bsell\b|\bshort\b|\bposition size\b|\bplace (?:an )?order\b",
    re.IGNORECASE,
)
RUNTIME_ROOT = Path(__file__).resolve().parents[3]


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiscussionCardError(f"{context} requires text")
    return value.strip()


def _statement(value: object, context: str) -> str:
    statement = _text(value, context)
    if TRADE_DIRECTIVE.search(statement):
        raise DiscussionCardError(f"{context} contains a trade directive")
    return statement


def _as_of_moment(value: object, context: str) -> tuple[str, datetime]:
    text = _text(value, context)
    if "T" not in text:
        raise DiscussionCardError(f"{context} requires a complete timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as error:
        raise DiscussionCardError(f"{context} requires an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DiscussionCardError(f"{context} timestamp requires a timezone")
    return text, parsed.astimezone(timezone.utc)


def _source(source: object, context: str) -> dict[str, str]:
    if not isinstance(source, dict):
        raise DiscussionCardError(f"{context} requires a source")
    record = {
        "name": _text(source.get("name"), f"{context} source"),
        "kind": _text(source.get("kind"), f"{context} source"),
        "as_of": _as_of_moment(source.get("as_of"), f"{context} source")[0],
        "url": _text(source.get("url"), f"{context} source"),
    }
    if record["kind"] not in SOURCE_KINDS:
        raise DiscussionCardError(f"{context} source kind is not allowed: {record['kind']}")
    return record


def _string_list(value: object, context: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DiscussionCardError(f"{context} must be a list")
    return [_statement(item, context) for item in value]


def _identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiscussionCardError("identity requires an object")
    record: dict[str, Any] = {"case_id": _text(value.get("case_id"), "identity case_id")}
    for field in ("issuer_id", "listing_id"):
        if value.get(field) is not None:
            record[field] = _text(value.get(field), f"identity {field}")
    return record


def _portable_path(value: object, context: str) -> str:
    """Accept only portable relative paths; never echo the rejected path."""
    path_text = _text(value, context)
    if (
        Path(path_text).is_absolute()
        or "\\" in path_text
        or ":" in path_text
    ):
        raise DiscussionCardError(f"{context} requires a portable relative path")
    if ".." in Path(path_text).parts:
        raise DiscussionCardError(f"{context} must not contain '..'")
    return path_text


def _inputs_declared(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiscussionCardError("inputs_declared requires an object")
    artifact_paths = value.get("artifact_paths") or []
    if not isinstance(artifact_paths, list) or not all(
        isinstance(item, str) and item.strip() for item in artifact_paths
    ):
        raise DiscussionCardError("inputs_declared artifact_paths must be a list of paths")
    raw_doc_id = value.get("drive_doc_id")
    raw_case_id = value.get("case_id")
    if raw_doc_id is not None and not isinstance(raw_doc_id, str):
        raise DiscussionCardError("inputs_declared drive_doc_id must be text")
    if raw_case_id is not None and not isinstance(raw_case_id, str):
        raise DiscussionCardError("inputs_declared case_id must be text")
    drive_doc_id = (raw_doc_id or "").strip()
    case_id = (raw_case_id or "").strip()
    if not artifact_paths and not drive_doc_id and not case_id:
        raise DiscussionCardError(
            "inputs_declared is empty: 未指定输入时只查当前工作区 manifest 的已登记 case "
            "并列出候选请用户选择，不扫描全库"
        )
    return {
        "artifact_paths": [
            _portable_path(item, "inputs_declared artifact_paths")
            for item in artifact_paths
        ],
        "drive_doc_id": drive_doc_id,
        "case_id": case_id,
    }


def _new_evidence(value: object, research_as_of: datetime) -> list[dict[str, Any]]:
    items = _string_list_or_empty(value)
    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise DiscussionCardError("new_evidence item must be an object")
        label = _statement(item.get("label"), "new_evidence")
        statement = _statement(item.get("statement"), f"{label} statement")
        source = _source(item.get("source"), label)
        _, source_moment = _as_of_moment(source["as_of"], f"{label} source")
        if source_moment > research_as_of:
            raise DiscussionCardError("source as_of is after research as_of")
        records.append({"label": label, "statement": statement, "source": source})
    return records


def _string_list_or_empty(value: object) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DiscussionCardError("expected a list")
    return value


def _plan_revision(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DiscussionCardError("plan_revision requires an object")
    plan_id = _text(value.get("plan_id"), "plan_revision plan_id")
    base_revision = value.get("base_revision")
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        raise DiscussionCardError("plan_revision base_revision requires a non-negative integer")
    return {
        "plan_id": plan_id,
        "base_revision": base_revision,
        "change_summary": _statement(
            value.get("change_summary"), "plan_revision change_summary"
        ),
        "trigger_evidence": _string_list(
            value.get("trigger_evidence"), "plan_revision trigger_evidence"
        ),
    }


def _is_ah_pair(first: str, second: str) -> bool:
    first_a = first.endswith(A_SHARE_SUFFIXES)
    second_a = second.endswith(A_SHARE_SUFFIXES)
    first_hk = first.endswith(HK_SUFFIX)
    second_hk = second.endswith(HK_SUFFIX)
    return (first_a and second_hk) or (first_hk and second_a)


def _is_version_one(value: object) -> bool:
    """Strict int 1: bool is an int subclass, so True must not pass."""
    return type(value) is int and value == 1


def _ref_identity(
    identity: object, card_identity: dict[str, Any], path_text: str
) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise DiscussionCardError(
            f"artifact_refs artifact identity requires an object: {path_text}"
        )
    record: dict[str, Any] = {}
    for field in ("issuer_id", "listing_id", "case_id"):
        record[field] = _text(
            identity.get(field), f"artifact_refs identity {field} ({path_text})"
        )
    for field in ("artifact_version", "schema_version"):
        version = identity.get(field)
        if not _is_version_one(version):
            raise DiscussionCardError(
                f"artifact_refs identity {field} must be 1: {path_text}"
            )
    if record["case_id"] != card_identity.get("case_id"):
        raise DiscussionCardError(
            f"artifact_refs identity case_id does not match card case_id: {path_text}"
        )
    card_issuer = card_identity.get("issuer_id")
    if card_issuer is None or record["issuer_id"] != card_issuer:
        raise DiscussionCardError(
            f"artifact_refs identity issuer_id does not match card issuer_id: {path_text}"
        )
    card_listing = card_identity.get("listing_id")
    if (
        card_listing is not None
        and record["listing_id"] != card_listing
        and not _is_ah_pair(record["listing_id"], card_listing)
    ):
        raise DiscussionCardError(
            "artifact_refs identity listing_id differs from card listing_id "
            f"without a legitimate A/H pair: {path_text}"
        )
    return record


def _artifact_refs(value: object, card_identity: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for raw in _string_list_or_empty(value):
        path_text = _portable_path(raw, "artifact_refs")
        path = Path(path_text)
        if path.is_symlink():
            raise DiscussionCardError(
                f"artifact_refs artifact must not be a symlink: {path_text}"
            )
        if not path.is_file():
            raise DiscussionCardError(f"artifact_refs artifact does not exist: {path_text}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            raise DiscussionCardError(
                f"artifact_refs artifact is not a readable JSON object: {path_text}"
            )
        if not _is_version_one(payload.get("schema_version")):
            raise DiscussionCardError(
                f"artifact_refs artifact schema_version must be 1: {path_text}"
            )
        record: dict[str, Any] = {"path": path_text}
        record["identity"] = _ref_identity(
            payload.get("identity"), card_identity, path_text
        )
        for summary_field in ("computed_as_of", "research_as_of", "model_version"):
            if isinstance(payload.get(summary_field), str):
                record[summary_field] = payload[summary_field]
        refs.append(record)
    return refs


def parse_inputs(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DiscussionCardError("input must be a JSON object")
    if not _is_version_one(payload.get("schema_version")):
        raise DiscussionCardError("schema_version must be 1")
    research_as_of, research_moment = _as_of_moment(
        payload.get("research_as_of"), "research_as_of"
    )
    identity = _identity(payload.get("identity"))
    inputs_declared = _inputs_declared(payload.get("inputs_declared"))
    declared_case_id = inputs_declared["case_id"]
    if declared_case_id and declared_case_id != identity["case_id"]:
        raise DiscussionCardError(
            "inputs_declared case_id does not match card identity case_id"
        )
    new_evidence = _new_evidence(payload.get("new_evidence"), research_moment)
    assumption_changes = _string_list(
        payload.get("assumption_changes"), "assumption_changes"
    )
    thesis_status = _text(payload.get("thesis_status"), "thesis_status")
    if thesis_status not in THESIS_STATUS:
        raise DiscussionCardError(f"thesis_status is not allowed: {thesis_status}")
    if not new_evidence and not assumption_changes and thesis_status != "unchanged":
        raise DiscussionCardError(
            "no new evidence or assumption changes requires thesis_status unchanged"
        )
    confidence = _text(payload.get("confidence"), "confidence")
    if confidence not in CONFIDENCE:
        raise DiscussionCardError(f"confidence is not allowed: {confidence}")
    escalation = payload.get("escalation_proposal")
    return {
        "identity": identity,
        "research_as_of": research_as_of,
        "inputs_declared": inputs_declared,
        "new_evidence": new_evidence,
        "assumption_changes": assumption_changes,
        "thesis_status": thesis_status,
        "counter_arguments": _string_list(
            payload.get("counter_arguments"), "counter_arguments"
        ),
        "open_questions": _string_list(payload.get("open_questions"), "open_questions"),
        "confidence": confidence,
        "plan_revision": _plan_revision(payload.get("plan_revision")),
        "artifact_refs": _artifact_refs(payload.get("artifact_refs"), identity),
        "escalation_proposal": (
            _statement(escalation, "escalation_proposal") if escalation is not None else None
        ),
        "action": (
            None
            if new_evidence or assumption_changes
            else NO_INCREMENT_ACTION
        ),
    }


def _bullet_list(items: list[str], empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {item}" for item in items]


def render_markdown(card: dict[str, Any]) -> str:
    identity = card["identity"]
    declared = card["inputs_declared"]
    lines = [
        f"# 投研讨论卡：{identity['case_id']}",
        "",
        f"研究截至：{card['research_as_of']}",
        "身份：" + "；".join(
            f"{field}={identity[field]}"
            for field in ("issuer_id", "listing_id", "case_id")
            if field in identity
        ),
        "",
        "## 输入声明",
    ]
    if declared["artifact_paths"]:
        lines.extend(f"- 本地 artifact：{path}" for path in declared["artifact_paths"])
    if declared["drive_doc_id"]:
        lines.append(f"- Drive 文档 ID：{declared['drive_doc_id']}")
    if declared["case_id"]:
        lines.append(f"- case_id：{declared['case_id']}")
    lines.extend(["", "## 新证据"])
    if card["new_evidence"]:
        for item in card["new_evidence"]:
            source = item["source"]
            lines.append(
                f"- **{item['label']}**：{item['statement']}"
                f"（来源：[{source['name']}]({source['url']})，"
                f"kind：{source['kind']}，as_of：{source['as_of']}）"
            )
    elif not card["assumption_changes"]:
        lines.append(f"- {NO_INCREMENT_ACTION}：未提供新证据与假设变化。")
    else:
        lines.append("- 本次未提供新证据。")
    lines.extend(["", "## 假设变化"])
    lines.extend(_bullet_list(card["assumption_changes"], "本次未记录假设变化。"))
    lines.extend(
        [
            "",
            "## 论点状态",
            f"- {THESIS_STATUS_LABELS[card['thesis_status']]}（{card['thesis_status']}）",
            "",
            "## 反方论证",
        ]
    )
    lines.extend(_bullet_list(card["counter_arguments"], "本次未记录反方论证。"))
    lines.extend(["", "## 待验证事项"])
    lines.extend(_bullet_list(card["open_questions"], "本次未记录待验证事项。"))
    lines.extend(
        [
            "",
            "## 置信度",
            f"- {CONFIDENCE_LABELS[card['confidence']]}（{card['confidence']}）",
            "",
            "## 方案 revision 提议",
        ]
    )
    revision = card["plan_revision"]
    if revision is None:
        lines.append("- 本次无方案 revision 提议。")
    else:
        lines.extend(
            [
                f"- plan_id：{revision['plan_id']}",
                f"- base_revision：{revision['base_revision']}",
                f"- 变更摘要：{revision['change_summary']}",
            ]
        )
        lines.extend(
            f"- 触发证据：{item}" for item in revision["trigger_evidence"]
        )
        lines.append(
            "- 本提议需经 Drive 工作台 confirm 才生效；本 skill 不直接写入 Drive。"
        )
    lines.extend(["", "## 引用 artifact"])
    if card["artifact_refs"]:
        for ref in card["artifact_refs"]:
            ref_identity = ref.get("identity") or {}
            detail = "；".join(
                f"{field}={ref_identity[field]}"
                for field in ("issuer_id", "listing_id", "case_id")
                if field in ref_identity
            )
            suffix = f"（{detail}）" if detail else ""
            lines.append(f"- {ref['path']}{suffix}")
        lines.append("- 引用仅读取 identity/摘要字段，不重算、不校验其内部数字。")
    else:
        lines.append("- 本次未引用既有 artifact。")
    lines.extend(["", "## 升级提议"])
    if card["escalation_proposal"]:
        lines.append(f"- {card['escalation_proposal']}")
        lines.append("- 升级提议仅为文字建议，不自动执行。")
    else:
        lines.append("- 无。")
    return "\n".join(lines).rstrip() + "\n"


def render_json(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "identity": card["identity"],
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "card_version": CARD_VERSION,
        "research_as_of": card["research_as_of"],
        "inputs_declared": card["inputs_declared"],
        "new_evidence": card["new_evidence"],
        "assumption_changes": card["assumption_changes"],
        "thesis_status": card["thesis_status"],
        "counter_arguments": card["counter_arguments"],
        "open_questions": card["open_questions"],
        "confidence": card["confidence"],
        "plan_revision": card["plan_revision"],
        "artifact_refs": card["artifact_refs"],
        "escalation_proposal": card["escalation_proposal"],
        "action": card["action"],
    }


def _write_new(path: Path, content: str) -> None:
    resolved = path.resolve()
    if RUNTIME_ROOT == resolved or RUNTIME_ROOT in resolved.parents:
        raise DiscussionCardError("output path must not be inside the Skill runtime package")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--json", type=Path, default=None)
    arguments = parser.parse_args()
    try:
        payload = json.loads(arguments.input.read_text(encoding="utf-8"))
        card = parse_inputs(payload)
        if arguments.json is not None:
            _write_new(
                arguments.json,
                json.dumps(render_json(card), ensure_ascii=False, indent=2) + "\n",
            )
        _write_new(arguments.output, render_markdown(card))
    except (OSError, json.JSONDecodeError, DiscussionCardError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
