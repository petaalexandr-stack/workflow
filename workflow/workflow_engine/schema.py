"""Simple ordered, dynamic module route. No DAG or background AI coordinators."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .modules import CATALOG


class WorkflowError(Exception):
    """Actionable validation or state error shown to the operator."""


WRITER_AGENTS = frozenset({"developer", "debugger", "documentation"})
ALL_AGENTS = frozenset({"orchestrator", *(role for spec in CATALOG.values() for role in spec.agents)})
MODULE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
MERMAID_PREFIX = re.compile(r"^\s*(flowchart|graph|sequenceDiagram|classDiagram|erDiagram|stateDiagram|journey)\b")


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def approval_token(run_id: str, sequence: int, kind: str, content: Any) -> str:
    payload = compact_json({"run": run_id, "sequence": sequence, "kind": kind, "content": content})
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def normalize_route(raw: list, design_mode: str = "none") -> list[dict[str, Any]]:
    """Normalize a linear, human-approved module list with explicit artifact inputs.

    Entries may be strings (template names) or {id,template,goal,inputs}. Modules
    only receive declared, EARLIER approved outputs. No dependency graph scheduler.
    """
    from .system_design import validate_design_choice
    if not isinstance(raw, list) or not raw or len(raw) > 64:
        raise WorkflowError("Маршрут должен содержать 1..64 модулей.")
    route: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"id": entry, "template": entry}
        if not isinstance(entry, dict):
            raise WorkflowError("Неверная структура модуля маршрута.")
        id_ = entry.get("id")
        template = entry.get("template", id_)
        if not isinstance(id_, str) or not MODULE_ID.fullmatch(id_) or id_ in seen:
            raise WorkflowError(f"Недопустимый или повторный id: {id_!r}.")
        if not isinstance(template, str) or template not in CATALOG:
            raise WorkflowError(f"Неизвестный модуль: {template!r}.")
        if template == "documentation" and id_ != "documentation":
            raise WorkflowError("Финальный модуль должен иметь id documentation.")
        if id_ == "documentation" and template != "documentation":
            raise WorkflowError("Имя documentation зарезервировано.")
        spec = CATALOG[template]
        goal = entry.get("goal") or spec.description
        if not isinstance(goal, str) or not 2 <= len(goal.strip()) <= 600:
            raise WorkflowError(f"Неверная цель модуля {id_}.")
        inputs = entry.get("inputs")
        if inputs is None:
            inputs = [r["id"] for r in route] if template == "documentation" else ([route[-1]["id"]] if route else [])
        if template == "documentation":
            # Documentation must audit ALL earlier modules irrespective of model output.
            inputs = [r["id"] for r in route]
        if not isinstance(inputs, list) or len(inputs) != len(set(map(str, inputs))):
            raise WorkflowError(f"Повторные или неверные входы для {id_}.")
        if any(not isinstance(ref, str) or ref not in seen for ref in inputs):
            raise WorkflowError(f"Модуль {id_} может читать только результаты ПРЕДЫДУЩИХ модулей.")
        route.append({"id": id_, "template": template, "agents": list(spec.agents),
                      "goal": goal.strip(), "inputs": inputs, "category": spec.category})
        seen.add(id_)
    if route[-1]["template"] != "documentation":
        if "documentation" in seen:
            raise WorkflowError("Documentation должен быть последним модулем.")
        route.append({"id": "documentation", "template": "documentation",
                      "agents": ["documentation"], "goal": CATALOG["documentation"].description,
                      "inputs": [r["id"] for r in route], "category": "documentation"})
    if any(r["template"] == "documentation" for r in route[:-1]):
        raise WorkflowError("Documentation разрешён только в конце.")
    validate_design_choice(["architect" if r["template"] == "system_design" else r["template"] for r in route], design_mode)
    for mod in route:
        if mod["template"] == "system_design":
            mod["design_mode"] = design_mode
    return route


def module_signature(module: dict[str, Any]) -> str:
    return compact_json({k: module.get(k) for k in ("template", "goal", "agents", "inputs", "design_mode")})


def mermaid_quote(value: str) -> str:
    return value.replace('"', "'").replace("<", "").replace(">", "").replace("\n", " ")[:110]


def route_diagram(modules: list[dict[str, Any]]) -> str:
    """Linear user presentation; agreements to the RIGHT, no fake DAG edges."""
    out = ['flowchart TB', '    U["Твоя идея или задание"] --> Q["Orchestrator: уточнение"]',
           '    Q -.-> G0["ТЫ: подтвердить понимание"]',
           '    Q --> R["Orchestrator: предложить динамический маршрут"]',
           '    R -.-> G1["ТЫ: утвердить маршрут"]']
    before = "R"
    for i, mod in enumerate(modules):
        name = f"M{i}"
        label = mermaid_quote(mod["id"] + " · " + ", ".join(mod["agents"]))
        out.extend([f'    {before} --> {name}["{label}"]',
                    f'    {name} -.-> G{i+2}["ТЫ: принять результат {mermaid_quote(mod["id"])}"]'])
        before = name
    out += [f'    {before} --> END["Завершено"]',
            '    classDef gate fill:#fff1cd,stroke:#c28d28,color:#543e12',
            '    class ' + ",".join(f"G{i}" for i in range(len(modules) + 2)) + ' gate']
    return "\n".join(out)


def stage_diagram(module: dict[str, Any]) -> str:
    """Compatibility with existing role providers: stage = one independent module."""
    out = ['flowchart TB', '    IN["Утверждённый вход модуля"]']
    for n, role in enumerate(module["agents"]):
        out += [f'    IN --> A{n}["{mermaid_quote(role)}"]', f'    A{n} --> RESULT["Результат модуля"]']
    out += ['    RESULT -.-> USER["ТЫ: согласование"]']
    return "\n".join(out)
