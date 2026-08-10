#!/usr/bin/env python3
"""Offline contract, package-budget, and fixture checks for the Mars runtime."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "skills" / "mars-research-assistant"
SKILLS = RUNTIME / "skills"
MANIFEST = RUNTIME / "mars-skills.json"
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
POLICY_BLOCK = re.compile(r"```mars-skill-policy\n(?P<payload>\{.*?\})\n```", re.DOTALL)
MAX_RUNTIME_FILES = 80
MAX_RUNTIME_BYTES = 3 << 19  # 1.5 MiB
MAX_FIXTURE_SECONDS = 1.0
LOCAL_ARTIFACT_CONTRACT = {
    "directory": "mars-research",
    "format": "markdown",
    "unique_name_required": True,
    "overwrite": "forbidden",
}
IDENTITY_REQUEST_FIELDS = ["company_name", "ticker", "exchange"]
IDENTITY_VERIFIED_FIELDS = [*IDENTITY_REQUEST_FIELDS, "issuer"]
PRIMARY_SOURCE_KINDS = ["sec_filing", "regulatory_filing", "issuer_ir", "exchange"]
UNDERWRITING_CHAPTERS = [
    "研究范围、预注册命题与交易结论",
    "公司、业务模式与价值驱动",
    "行业结构、竞争与行业专属反证",
    "管理层、治理与资本配置",
    "财务、分部/KPI 与财报质量",
    "预期差、催化剂、基准率与跟踪清单",
    "可复算估值与“现价定价了什么”",
    "反方论证、事前风险预演与可证伪条件",
    "来源、数据对账、时间戳、假设与数据缺口",
]
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[^/\s]+(?:/|$)")
SECRET_ASSIGNMENT = re.compile(
    r"(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*['\"]?\S+",
    re.IGNORECASE,
)


def _fail(message: str) -> None:
    raise SystemExit(f"Mars Skills verification failed: {message}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"invalid JSON: {path.relative_to(ROOT)} ({error})")
    if not isinstance(value, dict):
        _fail(f"JSON object required: {path.relative_to(ROOT)}")
    return value


DEVELOPMENT_DIR_PARTS = {"tests", "docs", ".git", ".venv", "__pycache__"}


def _runtime_files() -> list[Path]:
    """Runtime payload files only; local development directories never count."""
    if not RUNTIME.is_dir():
        _fail("runtime package is missing")
    return sorted(
        path
        for path in RUNTIME.rglob("*")
        if path.is_file()
        and not any(
            part in DEVELOPMENT_DIR_PARTS
            for part in path.relative_to(RUNTIME).parts
        )
    )


def _schema_version_is_one(value: object) -> bool:
    """Strict int 1 check; bool is rejected even though True == 1 in Python."""
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def _verify_runtime_budget() -> None:
    files = _runtime_files()
    size = sum(path.stat().st_size for path in files)
    if len(files) > MAX_RUNTIME_FILES:
        _fail(f"runtime package has {len(files)} files; maximum is {MAX_RUNTIME_FILES}")
    if size > MAX_RUNTIME_BYTES:
        _fail(f"runtime package is {size} bytes; maximum is {MAX_RUNTIME_BYTES}")
    prohibited = {"README.md", "AGENTS.md", "package-files.txt", "install-from-github.sh", "install-mars-skill.sh", "managed_package.py"}
    leaked = [path.relative_to(RUNTIME).as_posix() for path in files if path.name in prohibited]
    if leaked:
        _fail(f"development files leaked into runtime package: {', '.join(leaked)}")


def _verify_public_text() -> None:
    for path in [*ROOT.glob("README*"), *ROOT.glob("scripts/*"), *_runtime_files()]:
        if not path.is_file() or path.suffix in {".js", ".png"}:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.name == "uv.lock":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT)
        if PRIVATE_PATH.search(text):
            _fail(f"private absolute path: {relative}")
        if SECRET_ASSIGNMENT.search(text):
            _fail(f"possible credential assignment: {relative}")
        if "curl" in text.lower():
            _fail(f"curl installer reference remains in public runtime surface: {relative}")


def _verify_root_skill() -> None:
    root_skill = RUNTIME / "SKILL.md"
    text = root_skill.read_text(encoding="utf-8") if root_skill.is_file() else ""
    if "name: mars-research-assistant" not in text:
        _fail("runtime root Skill identity is invalid")
    command_markers = (
        "npx skills add archerthegoat/mars-research-assistant",
        "--skill mars-research-assistant",
        "--agent codex",
        "--global",
        "--copy",
    )
    if any(marker not in text for marker in command_markers):
        _fail("runtime root Skill does not provide the approved npx command")
    for identifier in EXPECTED_SKILLS:
        if f"`skills/{identifier}/SKILL.md`" not in text:
            _fail(f"runtime root Skill does not expose {identifier}")
    for marker in ("mars-market-preferences.json", "A/H 对比"):
        if marker not in text:
            _fail(f"runtime root Skill misses market onboarding marker: {marker}")


def _verify_manifest_and_skills() -> None:
    manifest = _read_json(MANIFEST)
    rows = manifest.get("skills")
    if not _schema_version_is_one(manifest.get("schema_version")) or not isinstance(rows, list):
        _fail("runtime manifest is invalid")
    if manifest.get("collection") != "Mars Research Assistant":
        _fail("runtime manifest collection is invalid")
    identifiers = {row.get("id") for row in rows if isinstance(row, dict)}
    if identifiers != EXPECTED_SKILLS or len(rows) != len(EXPECTED_SKILLS):
        _fail("runtime manifest must contain exactly the eight expected Skills")
    display_names = {row.get("id"): row.get("display_name") for row in rows if isinstance(row, dict)}
    if display_names.get("deep-equity-research") != "深度研究":
        _fail("deep-equity-research display name must stay 深度研究")
    if display_names.get("investment-analysis") != "投研分析":
        _fail("investment-analysis display name must be 投研分析")
    directories = {path.name for path in SKILLS.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()}
    if directories != EXPECTED_SKILLS:
        _fail("runtime Skill directories and manifest differ")
    for identifier in sorted(EXPECTED_SKILLS):
        directory = SKILLS / identifier
        skill = (directory / "SKILL.md")
        capability_path = directory / "capability.json"
        agent = directory / "agents" / "openai.yaml"
        if not skill.is_file() or not capability_path.is_file() or not agent.is_file():
            _fail(f"runtime Skill is incomplete: {identifier}")
        text = skill.read_text(encoding="utf-8")
        if f"name: {identifier}" not in text or "`capability.json`" not in text:
            _fail(f"runtime Skill metadata is invalid: {identifier}")
        if "tests/" in text:
            _fail(f"runtime Skill refers to development tests: {identifier}")
        capability = _read_json(capability_path)
        if not _schema_version_is_one(capability.get("schema_version")) or capability.get("skill") != identifier:
            _fail(f"capability identity is invalid: {identifier}")
        delivery = capability.get("delivery")
        forbidden = capability.get("forbidden_effects")
        if not isinstance(delivery, str) or not isinstance(forbidden, list):
            _fail(f"capability delivery boundary is invalid: {identifier}")
        match = POLICY_BLOCK.findall(text)
        if len(match) != 1:
            _fail(f"Skill policy is missing or ambiguous: {identifier}")
        if json.loads(match[0]) != {"delivery": delivery, "forbidden_effects": forbidden}:
            _fail(f"Skill policy and capability contradict: {identifier}")
        if "tests/" in capability_path.read_text(encoding="utf-8"):
            _fail(f"runtime capability refers to development tests: {identifier}")
        if "display_name:" not in agent.read_text(encoding="utf-8"):
            _fail(f"agent metadata is invalid: {identifier}")
        if capability.get("local_artifact_contract") != LOCAL_ARTIFACT_CONTRACT:
            _fail(f"textual Skill local artifact contract is invalid: {identifier}")
    quick = _read_json(SKILLS / "instrument-research" / "capability.json")
    if quick.get("delivery") != "local_markdown_equity_snapshot" or quick.get("issuer_identity_required_before_artifact") is not True:
        _fail("equity snapshot contract is incomplete")
    if quick.get("recent_company_updates", {}).get("window_days") != 30:
        _fail("equity snapshot must keep the 30-day update window")
    if quick.get("issuer_identity_contract") != {
        "request_fields": IDENTITY_REQUEST_FIELDS,
        "verified_fields": IDENTITY_VERIFIED_FIELDS,
        "exact_match_fields": IDENTITY_REQUEST_FIELDS,
    }:
        _fail("equity snapshot issuer identity contract is incomplete")
    if quick.get("source_hierarchy") != {
        "identity_and_financials": PRIMARY_SOURCE_KINDS,
        "price_and_valuation_anchor": ["public_quote"],
        "company_updates": ["issuer_announcement", "credible_media"],
    }:
        _fail("equity snapshot source hierarchy is invalid")
    ask = _read_json(SKILLS / "ask-mars" / "capability.json")
    guidance = ask.get("market_scope_guidance", {})
    if guidance.get("no_default_us") is not True or not guidance.get("preference_file"):
        _fail("ask-mars must follow the market preference without a US default")
    deep = _read_json(SKILLS / "deep-equity-research" / "capability.json")
    if deep.get("chapters") != UNDERWRITING_CHAPTERS:
        _fail("deep-equity-research chapters do not match the underwriting contract")
    if deep.get("research_modes") != ["initial", "earnings_update"]:
        _fail("deep-equity-research must declare initial and earnings_update modes")
    if deep.get("earnings_update_without_prior_model") != "auto_degrade_to_initial":
        _fail("earnings update without a prior model must degrade to initial coverage")
    if deep.get("artifact_identity", {}).get("required_fields") != [
        "issuer_id", "listing_id", "case_id", "artifact_version", "schema_version",
    ]:
        _fail("deep-equity-research artifact identity fields are incomplete")
    if deep.get("issuer_identity_contract") != {
        "request_fields": IDENTITY_REQUEST_FIELDS,
        "verified_fields": IDENTITY_VERIFIED_FIELDS,
        "exact_match_fields": IDENTITY_REQUEST_FIELDS,
    }:
        _fail("deep-equity-research issuer identity contract is incomplete")
    if deep.get("source_hierarchy") != {
        "primary": PRIMARY_SOURCE_KINDS,
        "research_evidence": ["issuer_announcement", "credible_media"],
        "price": ["public_quote"],
        "valuation_assumption": ["valuation_assumption"],
        "forbidden": ["search_summary"],
    }:
        _fail("deep-equity-research source hierarchy is invalid")
    trade_plan = deep.get("trade_plan", {})
    if trade_plan.get("allowed_actions") != [
        "entry_plan", "target_plan", "invalidation_plan", "watch",
    ] or trade_plan.get("direction") != "long_only":
        _fail("deep-equity-research trade plan contract is invalid")
    forbidden = set(deep.get("forbidden_effects", []))
    if not {"position_sizing", "short_selling", "account_access", "broker_write"} <= forbidden:
        _fail("deep-equity-research must forbid account, sizing and short effects")
    market_scope = deep.get("market_scope", {})
    if market_scope.get("no_default_us") is not True or market_scope.get("scopes") != [
        "us", "hk", "a_share", "ah_compare",
    ]:
        _fail("deep-equity-research market scope contract is invalid")
    reference = SKILLS / "deep-equity-research" / "reference"
    registry = _read_json(reference / "industry_registry.json")
    industries = registry.get("industries", [])
    if len(industries) != 16 or not registry.get("registry_version"):
        _fail("industry registry must define exactly sixteen versioned industries")
    for industry in industries:
        if any(not industry.get(key) for key in (
            "key_kpis", "history_fields", "forecast_drivers",
            "valuation_methods", "counter_evidence", "min_data",
        )):
            _fail(f"industry registry entry is incomplete: {industry.get('id')}")
    market_contracts = _read_json(reference / "market_contracts.json")
    if set(market_contracts.get("scopes", {})) != {"us", "hk", "a_share", "ah_compare"}:
        _fail("market contracts must cover us, hk, a_share and ah_compare")
    if not market_contracts.get("vie_adr"):
        _fail("market contracts must define the VIE/ADR clause")
    rules = _read_json(reference / "preregistered_rules.json")
    if not rules.get("rules_version") or not rules.get("gates"):
        _fail("preregistered rules must be versioned and list decision gates")
    eq_rules = _read_json(reference / "earnings_quality_rules.json")
    if not eq_rules.get("rules_version"):
        _fail("earnings quality rules must be versioned")
    analysis = _read_json(SKILLS / "investment-analysis" / "capability.json")
    if analysis.get("delivery") != "local_markdown_discussion_card":
        _fail("investment-analysis delivery contract is invalid")
    if analysis.get("escalation") != "proposal_only":
        _fail("investment-analysis must only propose escalation, never auto-invoke")
    analysis_forbidden = set(analysis.get("forbidden_effects", []))
    if not {"broad_search", "rerun_deep_research", "auto_invoke_other_skills"} <= analysis_forbidden:
        _fail("investment-analysis must forbid broad search and auto-invocation")
    drive = _read_json(SKILLS / "drive-writeback" / "capability.json")
    if drive.get("supported_operations") != ["initialize_workbench", "workbench_write"]:
        _fail("drive-writeback must expose exactly workbench initialization and workbench writes")
    retired = {"archive_completed_research", "archive_contract", "archive_routes"}
    if retired & set(drive) or "archive_completed_research" in json.dumps(drive):
        _fail("drive-writeback must not expose the retired research archive flow")
    workbench = drive.get("workbench_contract", {})
    if workbench.get("sections") != ["idea_log", "current_plan", "decision_log", "review_log"]:
        _fail("drive workbench must keep the four master-document sections")
    for identifier in EXPECTED_SKILLS - {"technical-analysis"}:
        contract = _read_json(SKILLS / identifier / "capability.json")
        if "local_artifact" not in contract.get("response_fields", []):
            _fail(f"textual Skill must declare a local artifact: {identifier}")


def _render(script: Path, fixture: Path, required_markers: tuple[str, ...]) -> float:
    with tempfile.TemporaryDirectory(prefix="mars-fixture-") as temporary:
        output = Path(temporary) / "mars-research" / "artifact.md"
        started = perf_counter()
        result = subprocess.run(
            [sys.executable, str(script), "--input", str(fixture), "--output", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed = perf_counter() - started
        if result.returncode != 0:
            _fail(f"fixture render failed: {script.name}: {result.stderr.strip()}")
        if not output.is_file():
            _fail(f"fixture render did not create an artifact: {script.name}")
        rendered = output.read_text(encoding="utf-8")
        for marker in required_markers:
            if marker not in rendered:
                _fail(f"fixture missing required marker {marker}: {script.name}")
        if elapsed > MAX_FIXTURE_SECONDS:
            _fail(f"fixture render exceeded {MAX_FIXTURE_SECONDS:.1f}s: {script.name} ({elapsed:.3f}s)")
    return elapsed


def _verify_renderers() -> tuple[float, float]:
    snapshot = _render(
        RUNTIME / "scripts" / "render_equity_snapshot.py",
        ROOT / "tests" / "fixtures" / "equity-snapshot-primary.json",
        ("# 个股快览：TEST", "## 关键公开数据", "## 最近 30 天公司相关公告或新闻", "as_of：", "## 数据缺口"),
    )
    underwriting = _render(
        SKILLS / "deep-equity-research" / "scripts" / "render_underwriting.py",
        ROOT / "tests" / "fixtures" / "underwriting-inputs-initial.json",
        (
            "# 深度研究：",
            "## 1. 研究范围、预注册命题与交易结论",
            "## 9. 来源、数据对账、时间戳、假设与数据缺口",
        ),
    )
    return snapshot, underwriting


def main() -> int:
    _verify_runtime_budget()
    _verify_public_text()
    _verify_root_skill()
    _verify_manifest_and_skills()
    snapshot_seconds, underwriting_seconds = _verify_renderers()
    files = _runtime_files()
    size = sum(path.stat().st_size for path in files)
    print(
        "Mars Skills contract ok: "
        f"{len(files)} runtime files, {size} bytes, "
        f"snapshot={snapshot_seconds:.3f}s, underwriting={underwriting_seconds:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
