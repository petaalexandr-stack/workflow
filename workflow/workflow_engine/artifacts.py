"""Durable module handoffs: approved artifacts, never other chats' transcripts."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .schema import WorkflowError, route_diagram
from .system_design import SECTION_LABELS, sections_for


def safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)[:64]


def atomic_text(target: Path, content: str) -> None:
    base = target
    while base.name != ".workflow" and base.parent != base:
        if base.is_symlink():
            raise WorkflowError("Запрещён путь Workflow через символическую ссылку.")
        base = base.parent
    if base.name != ".workflow" or base.is_symlink():
        raise WorkflowError("Артефакты должны храниться только в каталоге .workflow без ссылок.")
    target.parent.mkdir(parents=True, exist_ok=True)
    base = target
    while base.name != ".workflow":
        if base.is_symlink():
            raise WorkflowError("Путь артефакта изменился на символическую ссылку.")
        base = base.parent
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def root(project: Path, run_id: str) -> Path:
    return project / ".workflow" / "runs" / run_id


def module_dir(project: Path, run_id: str, module_id: str) -> Path:
    return root(project, run_id) / "modules" / safe_id(module_id)


def create(project: Path, run_id: str, request: str) -> None:
    atomic_text(root(project, run_id) / "request.md", "# Исходное задание\n\n" + request + "\n")


def save_route(project: Path, run_id: str, route: dict[str, Any]) -> None:
    base = root(project, run_id)
    diagram = route_diagram(route["modules"])
    atomic_text(base / "route.json", json.dumps(route, ensure_ascii=False, indent=2) + "\n")
    atomic_text(base / "route.mmd", diagram + "\n")
    lines = ["# Динамический маршрут модулей", "", "*Это упорядоченный список, не DAG.*", "",
             f"Режим System Design: {route['system_design_mode']}", "",
             "```mermaid", diagram, "```", ""]
    for index, mod in enumerate(route["modules"], 1):
        lines.extend([f"## {index}. {mod['id']} — {mod['template']}", "", mod["goal"], "",
                      "Входные утверждённые модули: " + (", ".join(mod["inputs"]) or "нет"), ""])
    atomic_text(base / "route.md", "\n".join(lines))


def save_decisions(project: Path, run_id: str, decisions: list[dict[str, str]]) -> None:
    lines = ["# Согласованные решения", ""]
    for row in decisions:
        lines.append(f"- **{row['module']}**: {row['note']}")
    atomic_text(root(project, run_id) / "decisions.md", "\n".join(lines) + "\n")


def save_scoped_decisions(project: Path, run_id: str, module_id: str, decisions: list[dict[str, str]]) -> str:
    """Per-module snapshot; unrelated approvals are not included in the handoff."""
    rel = f".workflow/runs/{run_id}/modules/{safe_id(module_id)}/input-decisions.md"
    lines = ["# Разрешённые для модуля утверждённые решения", ""]
    lines.extend(f"- **{row['module']}**: {row['note']}" for row in decisions)
    if not decisions:
        lines.append("Нет дополнительных решений предыдущих модулей.")
    atomic_text(project / rel, "\n".join(lines) + "\n")
    return rel

def save_module(project: Path, run_id: str, mod: dict[str, Any], entry: dict[str, Any]) -> None:
    base = module_dir(project, run_id, mod["id"])
    report = entry.get("report") or {}
    lines = [f"# {mod['id']}: {mod['goal']}", "", "**Approval:** " +
             ("APPROVED" if entry.get("approved") else "WAITING / BLOCKED"), "",
             f"**Status:** {report.get('status', 'unknown')}", "", "## Краткий результат", "",
             report.get("summary", ""), "", "## Проверяемые доказательства", ""]
    lines.extend("- " + str(x) for x in report.get("evidence", []))
    lines.extend(["", "## Решения для согласования", ""])
    lines.extend("- " + str(x) for x in report.get("decisions", []))
    lines.extend(["", "## Риски", ""])
    lines.extend("- " + str(x) for x in report.get("risks", []))
    lines.extend(["", "## Mermaid", "", "```mermaid", report.get("mermaid", ""), "```", ""])
    if entry.get("approved"):
        lines.extend(["## Принято пользователем", "", entry.get("approval_note") or "Результат принят.", ""])
    atomic_text(base / "handoff.md", "\n".join(lines))
    atomic_text(base / "diagram.mmd", report.get("mermaid", "") + "\n")
    atomic_text(base / "result.json", json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
    if mod["template"] == "system_design":
        result = entry.get("agent_results", {}).get("architect", {})
        mode = mod.get("design_mode", "light")
        design = result.get("system_design") or {}
        status = "APPROVED" if entry.get("approved") else "DRAFT — pending approval"
        content = ["# System Design", "", f"**Status:** {status}", f"**Mode:** {mode}", ""]
        for section in sections_for(mode):
            content += [f"## {SECTION_LABELS[section]}", "", str(design.get(section, "_Missing_")), ""]
        content += ["## Mermaid", "", "```mermaid", str(result.get("mermaid", "")), "```", ""]
        atomic_text(base / "system-design.md", "\n".join(content))


def supersede(project: Path, run_id: str, module_id: str) -> None:
    """Keep audit evidence, but never present a stale approval as current."""
    for filename in ("handoff.md", "system-design.md"):
        path = module_dir(project, run_id, module_id) / filename
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            content = content.replace("**Approval:** APPROVED", "**Approval:** SUPERSEDED")
            content = content.replace("**Status:** APPROVED", "**Status:** SUPERSEDED")
            atomic_text(path, content)
