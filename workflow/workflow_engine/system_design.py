"""System Design contract: compact light design or validated full design.

Validation checks the presence and basic structure of the spec, NOT the truth of
architectural claims. An approval gate is still required for human decisions.
"""
from __future__ import annotations

import re
from typing import Any

from .schema import WorkflowError

SYSTEM_DESIGN_MODES = ("light", "full", "none")

SECTION_LABELS: dict[str, str] = {
    "scope": "Цель, границы и допущения",
    "hld": "High-Level Design — компоненты и их ответственность",
    "interfaces": "API, контракты и интеграции",
    "technologies": "Технологии и обоснование выбора",
    "dependencies": "Зависимости компонентов",
    "constraints": "Ограничения системы и окружения",
    "tradeoffs": "Альтернативы, компромиссы и решения для согласования",
    "lld": "Low-Level Design — модули и внутренние интерфейсы",
    "data_model": "Данные и хранилища",
    "scalability": "Нагрузка, ёмкость и масштабирование",
    "reliability": "Отказоустойчивость, восстановление и деградация",
    "performance_slo": "Производительность, метрики и SLO",
    "security": "Угрозы, права и меры защиты",
    "deployment": "Развёртывание, миграции и откат",
    "observability": "Логи, метрики, трассировка и оповещения",
}
LIGHT_SECTIONS = (
    "scope", "hld", "interfaces", "technologies", "dependencies", "constraints", "tradeoffs",
)
FULL_SECTIONS = LIGHT_SECTIONS + (
    "lld", "data_model", "scalability", "reliability", "performance_slo",
    "security", "deployment", "observability",
)


def validate_design_choice(stage_ids: list[str], mode: str) -> str:
    """Reject a mismatched design mode before an operator can approve the route."""
    if mode not in SYSTEM_DESIGN_MODES:
        raise WorkflowError("Режим System Design должен быть light, full или none.")
    has_architect = "architect" in stage_ids
    if has_architect and mode == "none":
        raise WorkflowError("Маршрут с Architect требует выбрать режим System Design: light или full.")
    if not has_architect and mode != "none":
        raise WorkflowError("Режим System Design допустим только при наличии этапа architect.")
    return mode


def sections_for(mode: str) -> tuple[str, ...]:
    if mode == "light":
        return LIGHT_SECTIONS
    if mode == "full":
        return FULL_SECTIONS
    raise WorkflowError("Для Architect нужен режим light или full.")


def architect_result_schema(mode: str, regular_schema: dict[str, Any]) -> dict[str, Any]:
    """Mode-specific strict JSON schema: no unnecessary full-design fields in light."""
    required_sections = sections_for(mode)
    props = {name: {"type": "string"} for name in required_sections}
    design_schema = {"type": "object", "properties": props,
                     "required": list(props), "additionalProperties": False}
    return {**regular_schema,
            "properties": {**regular_schema["properties"], "system_design": design_schema},
            "required": [*regular_schema["required"], "system_design"]}


def validate_architect_result(result: dict[str, Any], mode: str) -> list[str]:
    """Deterministic checklist; intentionally not a semantic or security audit."""
    required_sections = sections_for(mode)
    errors = []
    design = result.get("system_design")
    if not isinstance(design, dict):
        return ["Не представлена структурированная спецификация system_design."]
    for key in required_sections:
        value = design.get(key)
        if not isinstance(value, str) or len(value.strip()) < 16:
            errors.append(f"Не заполнен раздел: {SECTION_LABELS[key]} ({key}).")
        elif value.strip().lower() in {"not applicable", "не применимо", "todo", "tbd", "n/a"}:
            errors.append(f"Для неприменимого раздела {key} нужно указать обоснование.")
    mermaid = result.get("mermaid")
    if not isinstance(mermaid, str) or not re.match(
        r"^\s*(flowchart\s+(TB|TD|LR|RL|BT)|sequenceDiagram|classDiagram|erDiagram|stateDiagram(?:-v2)?)\b",
        mermaid,
    ):
        errors.append("Отсутствует Mermaid-диаграмма архитектуры в допустимом формате.")
    elif len(mermaid.strip()) < 40 or not any(arrow in mermaid for arrow in ("-->", "->>", "--", "||--")):
        errors.append("Mermaid слишком краткая: нужны компоненты и их связи.")
    elif "Задание для блока" in mermaid or "Отчёт блока" in mermaid:
        errors.append("Вместо архитектуры передана служебная диаграмма этапа.")
    return errors


def example_design(mode: str) -> dict[str, str]:
    """Explicitly fake content for deterministic demo mode only."""
    return {key: f"DEMO: {SECTION_LABELS[key]} — пример раздела без анализа реального проекта."
            for key in sections_for(mode)}
