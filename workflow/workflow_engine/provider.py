"""Demo and real OpenAI Responses adapters; each assignment starts a fresh context."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .schema import WorkflowError, stage_diagram, ALL_AGENTS
from .modules import CATALOG
from .tools import WorkspaceTools, parse_agent_tools
from .system_design import architect_result_schema, example_design


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def strings() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


UNDERSTANDING_SCHEMA = object_schema({
    "summary": {"type": "string"},
    "questions": strings(),
    "in_scope": strings(),
    "out_of_scope": strings(),
    "acceptance": strings(),
    "assumptions": strings(),
})
ROUTE_NODE_SCHEMA = object_schema({
    "id": {"type": "string"},
    "template": {"type": "string"},
    "inputs": strings(),
    "goal": {"type": "string"},
})
ROUTE_SCHEMA = object_schema({
    "modules": {"type": "array", "items": ROUTE_NODE_SCHEMA},
    "rationale": {"type": "string"},
    "risks": strings(),
    "system_design_mode": {"type": "string", "enum": ["light", "full", "none"]},
})
AGENT_RESULT_SCHEMA = object_schema({
    "status": {"type": "string", "enum": ["completed", "blocked", "needs_decision"]},
    "summary": {"type": "string"},
    "evidence": strings(),
    "mermaid": {"type": "string"},
    "decisions": strings(),
    "risks": strings(),
    "docs_handoff": {"type": "string"},
    "changed_files": strings(),
    "no_changes_needed": {"type": "boolean"},
})
STEP_REVIEW_SCHEMA = object_schema({
    "summary": {"type": "string"},
    "decisions_for_user": strings(),
    "risks": strings(),
    "next_step": {"type": "string"},
    "mermaid": {"type": "string"},
})
def packaged_config_root() -> Path:
    """Source checkout uses root AGENTS.md; installed wheel uses bundled config."""
    root = Path(__file__).resolve().parent.parent
    if (root / "AGENTS.md").is_file() and (root / "agents").is_dir():
        return root
    return Path(__file__).resolve().parent / "config"


class AgentRegistry:
    def __init__(self, config_root: Path):
        self.root = config_root
        self.global_policy = (config_root / "AGENTS.md").read_text(encoding="utf-8")

    def role(self, name: str) -> str:
        if name not in ALL_AGENTS:
            raise WorkflowError("Неизвестная роль: " + name)
        return (self.root / "agents" / f"{name}.md").read_text(encoding="utf-8")


class DemoProvider:
    """Explicit fake results for testing the state machine; NEVER performs development."""
    is_demo = True

    def understanding(self, request: str, answers: list[str], registry: AgentRegistry) -> dict[str, Any]:
        if len(request.strip()) < 13 and not answers:
            return {"summary": "Недостаточно данных", "questions": ["Какой именно результат требуется и по каким критериям его принять?"],
                    "in_scope": [], "out_of_scope": [], "acceptance": [], "assumptions": []}
        return {"summary": request + (("; уточнения: " + "; ".join(answers)) if answers else ""),
                "questions": [], "in_scope": ["Согласовать и смоделировать заявленную задачу"],
                "out_of_scope": ["Реальные изменения не выполняются в режиме demo"],
                "acceptance": ["Утверждены этапы и документы"], "assumptions": []}

    def route(self, understanding: dict[str, Any], registry: AgentRegistry, feedback: str = "") -> dict[str, Any]:
        # Demonstration ONLY. Heuristics are not real project analysis.
        text = (understanding.get("summary", "") + " " + feedback).lower()
        if any(word in text for word in ("bug", "баг", "ошибк", "исправ")):
            route = ["debugging", "development", "quality", "documentation"]
        elif any(word in text for word in ("документац", "readme", "documentation")):
            route = ["documentation"]
        else:
            route = ["discovery", "evals", "planning", "system_design", "tasks", "development", "quality", "documentation"]
        modules = []
        for i, name in enumerate(route):
            modules.append({"id": name, "template": name, "goal": CATALOG[name].description,
                            "inputs": [route[i - 1]] if i else []})
        return {"modules": modules, "rationale": "DEMO: это искусственный маршрут, не реальное планирование.",
                "risks": ["Проверка фактической корректности отсутствует в режиме demo."],
                "system_design_mode": "light" if "system_design" in route else "none"}

    def review_step(self, brief: dict[str, Any], registry: AgentRegistry) -> dict[str, Any]:
        return {"summary": "DEMO: результаты шага смоделированы; настоящих изменений нет.",
                "decisions_for_user": brief.get("decisions", []),
                "risks": brief.get("risks", []), "next_step": "Дальше только после утверждения.",
                "mermaid": brief.get("mermaid", "flowchart TD\n    A[Шаг] --> B[Согласование]")}

    def run_agent(self, agent: str, context: dict[str, Any], registry: AgentRegistry,
                  *, project: Path, allow_writes: bool, allow_shell: bool) -> dict[str, Any]:
        stage = context["stage"]
        result = {"status": "completed", "summary": f"DEMO: {agent} — этап {stage['goal']} смоделирован, НЕ выполнен.",
                "evidence": ["DEMO: реальные проверки и действия не проводились"],
                "mermaid": stage_diagram(stage), "decisions": [], "risks": [],
                "docs_handoff": "DEMO: нет подтверждённых изменений", "changed_files": [],
                "no_changes_needed": agent == "documentation", "simulated": True}
        if agent == "architect":
            mode = context["design_mode"]
            result["system_design"] = example_design(mode)
            result["mermaid"] = ('flowchart TD\n    Client["DEMO Client"] --> API["DEMO API"]\n'
                                 '    API --> Database["DEMO Storage"]')
        return result


class OpenAIProvider:
    is_demo = False

    def __init__(self, model: str = "gpt-5.4", *, api_key: str | None = None, api_base: str = "https://api.openai.com/v1"):
        self.model = model
        self.key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.api_base = api_base.rstrip("/")
        if not self.key:
            raise WorkflowError("Для режима openai требуется переменная OPENAI_API_KEY.")

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.api_base}/responses", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=150) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")[:600]
            raise WorkflowError(f"OpenAI API HTTP {exc.code}: {raw}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise WorkflowError(f"OpenAI API недоступен: {exc}") from exc
        if result.get("status") not in ("completed", None):
            raise WorkflowError(f"Ответ модели не завершён: {result.get('status')}")
        return result

    @staticmethod
    def _extract_text(result: dict[str, Any]) -> str:
        texts = []
        for item in result.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        texts.append(content.get("text", ""))
        value = "\n".join(texts) or result.get("output_text", "")
        if not value:
            raise WorkflowError("Модель не вернула структурированный текст.")
        return value

    def _json(self, name: str, instructions: str, data: dict[str, Any], schema: dict[str, Any],
              tools: WorkspaceTools | None = None) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = [{"role": "user", "content": json.dumps(data, ensure_ascii=False)}]
        definitions = tools.definitions() if tools is not None else []
        for round_no in range(12):
            if len(json.dumps(input_items, ensure_ascii=False)) > 180_000:
                raise WorkflowError("Контекст вызова превысил безопасный лимит: завершите задачу меньшими блоками.")
            payload = {
                "model": self.model,
                "instructions": instructions + "\nВерни итог как JSON согласно схеме; не утверждай, что проверка выполнена, если она не выполнялась.",
                "input": input_items,
                "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
                "store": False,
                "max_output_tokens": 12000,
            }
            if definitions:
                payload["tools"] = definitions
                payload["tool_choice"] = "auto"
            response = self._request(payload)
            calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
            if not calls:
                try:
                    return json.loads(self._extract_text(response))
                except (ValueError, TypeError) as exc:
                    raise WorkflowError(f"Некорректный JSON от модели: {exc}") from exc
            if tools is None:
                raise WorkflowError("Модель запросила инструмент там, где инструменты запрещены.")
            # Tool calls are local to this invocation: never reuse another agent's history.
            input_items.extend(response["output"])
            for call in calls:
                try:
                    args = json.loads(call.get("arguments", "{}"))
                    result = tools.execute(call["name"], args)
                except (ValueError, TypeError, KeyError) as exc:
                    result = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
                input_items.append({"type": "function_call_output", "call_id": call["call_id"], "output": result})
        raise WorkflowError("Превышен лимит 12 циклов инструментов в одной задаче.")

    def understanding(self, request: str, answers: list[str], registry: AgentRegistry) -> dict[str, Any]:
        instructions = registry.global_policy + "\n" + registry.role("orchestrator") + "\n" + (
            "На этапе уточнения НЕ исследуй файлы, НЕ планируй реализацию и НЕ вызывай других агентов. "
            "Задавай только существенные вопросы; если цель ясна, возвращай пустой список questions. "
            "Исключай домыслы о невысказанных требованиях."
        )
        return self._json("workflow_understanding", instructions,
                          {"request": request, "clarification_answers": answers}, UNDERSTANDING_SCHEMA)

    def route(self, understanding: dict[str, Any], registry: AgentRegistry, feedback: str = "") -> dict[str, Any]:
        instructions = registry.global_policy + "\n" + registry.role("orchestrator") + "\n" + (
            "Составь динамический УПОРЯДОЧЕННЫЙ СПИСОК независимых модулей для утверждённого задания. "
            "Это НЕ DAG. Не используй depends_on, параллельные ветки, блоки/координаторов. "
            "Для каждого модуля дай уникальный id, template из каталога, конкретную цель и inputs: "
            "id РАНЕЕ расположенных модулей, чьи утверждённые артефакты нужны ему. "
            "Модуль не может запускать другой модуль. Не передавай истории чатов. "
            "Не добавляй лишние этапы; повторять template можно с новым id. "
            "В конце обязательно documentation, даже если ожидается, что изменений не будет. "
            "Для system_design укажи режим light/full; иначе none. "
            "Никаких исследований и выполнения на стадии выбора маршрута; нужны согласования пользователя.")
        return self._json("workflow_route", instructions,
                          {"approved_understanding": understanding, "route_feedback": feedback,
                           "available_modules": [{"template": spec.name, "agents": spec.agents,
                                                  "purpose": spec.description}
                                                 for spec in CATALOG.values()],
                           "system_design_modes": ["light", "full", "none"]}, ROUTE_SCHEMA)

    def review_step(self, brief: dict[str, Any], registry: AgentRegistry) -> dict[str, Any]:
        instructions = (registry.global_policy + "\n" + registry.role("orchestrator") +
                        "\nЭто ТОЛЬКО подготовка отчёта для согласования пользователем. "
                        "НЕ запускай агентов, НЕ меняй маршрут или одобренные решения. "
                        "Обобщи проверяемые результаты текущего шага, спорные решения, риски; "
                        "покажи Mermaid по ФАКТИЧЕСКИМ результатам, не выдавай предложения за выполненную работу. "
                        "Ни на чём не основывайся кроме предоставленных кратких отчётов и доказательств.")
        return self._json("workflow_step_review", instructions, brief, STEP_REVIEW_SCHEMA)

    def run_agent(self, agent: str, context: dict[str, Any], registry: AgentRegistry,
                  *, project: Path, allow_writes: bool, allow_shell: bool) -> dict[str, Any]:
        prompt = registry.role(agent)
        approved_paths = {context["all_decisions_artifact"]}
        approved_paths.update(row["artifact"] for row in context.get("prior_approved_handoffs", []))
        approved_paths.update(context.get("approved_system_design_artifacts", []))
        if context.get("approved_system_design_artifact"):
            approved_paths.add(context["approved_system_design_artifact"])
        tools = WorkspaceTools(project, parse_agent_tools(prompt), write_allowed=allow_writes,
                               shell_allowed=allow_shell, approved_artifacts=approved_paths,
                               git_base_commit=(context.get("git_scope", {}).get("base_commit")
                                                if agent == "documentation" else None),
                               unsafe_shell=bool(context.get("runtime_unsafe_shell", False)))
        instructions = (registry.global_policy + "\n" + prompt + "\n"
                        "Ты работаешь в НОВОЙ, изолированной сессии. "
                        "Не меняй утверждённые решения и план. Если нужна новая существенная договорённость, "
                        "верни status=needs_decision. Mermaid должен описывать реальные или явно помеченные предлагаемые результаты. "
                        "При недостаточных правах верни blocked и объясни блокировку. "
                        "Используй доступные инструменты для фактических действий; не выдавай текст за изменённый код.")
        response_schema = AGENT_RESULT_SCHEMA
        if agent == "architect":
            mode = context["design_mode"]
            response_schema = architect_result_schema(mode, AGENT_RESULT_SCHEMA)
            instructions += ("\nРежим System Design: " + mode + ". Заполни ВСЕ поля system_design "
                             "как конкретные технические решения, основанные на доступных данных; "
                             "если область неприменима, объясни почему и какие предположения сделаны. "
                             "mermaid должен показывать архитектуру, а не маршрут агентов. "
                             "Не придумывай подтверждённые SLO/нагрузку: отмечай оценку и необходимость подтверждения. "
                             "Значимые альтернативы и решения выведи также в decisions для пользователя.")
        result = self._json("workflow_agent_result", instructions, context, response_schema, tools)
        result["tool_evidence"] = list(tools.evidence)
        # Write evidence overrides unsupported claims about changed files.
        result["changed_files"] = list(dict.fromkeys(tools.changed_files))
        result["simulated"] = False
        if agent in {"developer", "debugger", "documentation"} and result.get("status") == "completed":
            if not tools.changed_files and not result.get("no_changes_needed"):
                result["status"] = "blocked"
                result.setdefault("risks", []).append(
                    "Нет доказанных изменений файлов; укажите no_changes_needed с обоснованием "
                    "или выполните разрешённые действия через инструменты.")
            elif not tools.changed_files and not tools.evidence:
                result["status"] = "blocked"
                result.setdefault("risks", []).append(
                    "Отсутствуют действия инструментов для подтверждения отсутствия изменений.")
        return result


class ApprovalOnlyProvider:
    """No model access is needed to view or approve an existing live run."""
    is_demo = False
    provider_name = "approval_only"

    def __getattr__(self, name: str):
        raise WorkflowError("Эта операция требует активного AI-провайдера.")
