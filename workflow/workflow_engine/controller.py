"""Single deterministic Module Controller for Workflow v0.7.

Only Orchestrator proposes understanding, route and summaries; humans approve.
No DAG, Block Runner, Block Coordinator or hidden transitions. Each standalone
module runs its selected named Codex chats, emits a reviewable handoff, and
returns to the SAME approval gate. Durable state is independent of chat history.
"""
from __future__ import annotations

import os
import socket
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import artifacts
from .modules import CATALOG
from .provider import AgentRegistry
from .schema import WorkflowError, approval_token, mermaid_quote, module_signature, normalize_route


def shorten(value: Any, limit: int = 500) -> str:
    text = str(value)
    return text[:limit] + " …[см. артефакт]" if len(text) > limit else text


def git(project: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                 *args], cwd=project, capture_output=True, text=True, timeout=12)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_scope(project: Path, base: dict[str, Any]) -> dict[str, Any]:
    paths = set()
    commit = base.get("base_commit")
    if commit:
        paths.update(git(project, "diff", "--name-only", commit, "HEAD").splitlines())
    for args in (("diff", "--name-only"), ("diff", "--cached", "--name-only"),
                 ("ls-files", "--others", "--exclude-standard")):
        paths.update(git(project, *args).splitlines())
    paths = {p for p in paths if p and not p.startswith(".workflow/")}
    return {"base_commit": commit, "task_head": git(project, "rev-parse", "HEAD") or None,
            "changed_paths_potentially_in_task": sorted(paths)[:250],
            "initial_dirty_files": base.get("initial_dirty_files", []),
            "warning": "До начала задачи были незакоммиченные изменения; они не обязательно относятся к задаче."
            if base.get("initial_dirty_files") else ""}


class ModuleController:
    def __init__(self, store: Any, registry: AgentRegistry, provider: Any):
        self.store = store
        self.registry = registry
        self.provider = provider

    def load(self, run_id: str) -> dict[str, Any]:
        state = self.store.get(run_id)
        if state.get("schema_version") != 7:
            raise WorkflowError("Этот запуск создан до модульной версии v0.7. "
                                "Его нельзя незаметно переводить на новую семантику согласований. "
                                "Продолжайте в v0.6 или создайте новый запуск.")
        return state

    def new(self, project: Path, request: str, *, model: str = "default") -> dict[str, Any]:
        project = project.resolve()
        if not project.is_dir() or not request.strip():
            raise WorkflowError("Нужны существующий проект и непустое задание.")
        if len(request.encode("utf-8")) > 100_000:
            raise WorkflowError("Задание слишком длинное; сохраните подробности в ideas/*.md.")
        run_id = uuid.uuid4().hex[:12]
        provider_name = getattr(self.provider, "provider_name", "demo" if self.provider.is_demo else "openai")
        state = {
            "schema_version": 7, "id": run_id, "version": 0, "created_at": datetime.now(timezone.utc).isoformat(),
            "project": str(project), "request": request, "provider": provider_name, "model": model,
            "status": "understanding_draft", "answers": [], "understanding": None,
            "understanding_approved": False, "route_approved": False,
            "route": None, "route_feedback": "", "results": {}, "decisions": [],
            "gate": None, "gate_counter": 0, "current_module": None,
            "running_owner": None, "pending_replan": None,
            "git": {"base_commit": git(project, "rev-parse", "HEAD") or None,
                    "initial_dirty_files": [x for x in git(project, "status", "--porcelain").splitlines()
                                            if ".workflow/" not in x][:100]},
        }
        self.store.create(state)
        artifacts.create(project, run_id, request)
        artifacts.save_decisions(project, run_id, [])
        return state

    def _gate(self, state: dict, kind: str, payload: Any) -> None:
        state["gate_counter"] += 1
        state["gate"] = {"kind": kind, "token": approval_token(state["id"], state["gate_counter"], kind, payload)}

    @staticmethod
    def _verify(state: dict, gate: str, kind: str) -> None:
        row = state.get("gate")
        if not row or row["kind"] != kind or row["token"] != gate:
            raise WorkflowError("Недействительное или устаревшее согласование. Сначала посмотрите status.")

    def _normalize_proposal(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise WorkflowError("Orchestrator не вернул маршрут.")
        mode = data.get("system_design_mode", "none")
        modules = normalize_route(data.get("modules", []), mode)
        return {"modules": modules, "system_design_mode": mode,
                "rationale": shorten(data.get("rationale", ""), 3000),
                "risks": [shorten(x) for x in data.get("risks", []) if isinstance(x, str)][:25]}

    def current(self, state: dict) -> dict | None:
        if not state.get("route"):
            return None
        for mod in state["route"]["modules"]:
            if not state["results"].get(mod["id"], {}).get("approved"):
                return mod
        return None

    def next(self, run_id: str, *, allow_writes: bool = False, allow_shell: bool = False,
             unsafe_shell: bool = False) -> dict:
        state = self.load(run_id)
        status = state["status"]
        if status == "understanding_draft":
            info = self.provider.understanding(state["request"], state["answers"], self.registry)
            if not isinstance(info, dict) or not isinstance(info.get("summary"), str) or not info["summary"].strip():
                raise WorkflowError("Orchestrator не сформулировал понимание задания.")
            questions = info.get("questions")
            if not isinstance(questions, list) or any(not isinstance(q, str) for q in questions):
                raise WorkflowError("Orchestrator вернул некорректные вопросы.")
            if not questions and (not isinstance(info.get("acceptance"), list) or not info["acceptance"]):
                raise WorkflowError("Нельзя согласовать задание без критериев приёмки.")
            state["understanding"] = info
            if questions:
                state["status"] = "await_clarification"
                state["gate"] = None
                action = "clarification_questions"
            else:
                state["status"] = "await_understanding"
                self._gate(state, "understanding", info)
                action = "understanding_proposed"
            self.store.save(state, action)
            return state
        if status == "route_draft":
            if not state["understanding_approved"]:
                raise WorkflowError("Сначала необходимо подтверждение понимания.")
            proposal = self.provider.route(state["understanding"], self.registry, state["route_feedback"])
            state["route"] = self._normalize_proposal(proposal)
            state["route_approved"] = False
            state["status"] = "await_route"
            self._gate(state, "route", state["route"])
            self.store.save(state, "route_proposed")
            artifacts.save_route(Path(state["project"]), run_id, state["route"])
            return state
        if status == "ready_module":
            if not (state["understanding_approved"] and state["route_approved"]):
                raise WorkflowError("Нет двух обязательных начальных согласований.")
            mod = self.current(state)
            if mod is None:
                raise WorkflowError("Модули уже завершены.")
            return self._run(state, mod, allow_writes=allow_writes,
                             allow_shell=allow_shell, unsafe_shell=unsafe_shell)
        if status == "running_module":
            raise WorkflowError("Выполнение прервано или ещё идёт. Проверьте чат и Git; затем recover.")
        raise WorkflowError(f"При статусе {status} требуется явное решение пользователя, не next.")

    def answer(self, run_id: str, text: str) -> dict:
        state = self.load(run_id)
        if state["status"] not in ("await_clarification", "await_understanding"):
            raise WorkflowError("Сейчас нет этапа уточнения.")
        if not text.strip():
            raise WorkflowError("Нужен непустой ответ.")
        state["answers"].append(text.strip())
        state["understanding_approved"] = False
        state["gate"] = None
        state["status"] = "understanding_draft"
        self.store.save(state, "clarification_updated")
        return state

    def route_feedback(self, run_id: str, text: str) -> dict:
        state = self.load(run_id)
        if state["status"] != "await_route" or not text.strip():
            raise WorkflowError("Корректировка маршрута доступна только при его согласовании.")
        state["route_feedback"] = text.strip()
        state["route_approved"] = False
        state["status"] = "route_draft"
        state["gate"] = None
        self.store.save(state, "route_feedback_requested")
        return state

    def set_route(self, run_id: str, proposal: dict) -> dict:
        """Manual route amendment still demands NEW approval; does not start agents."""
        state = self.load(run_id)
        if state["status"] != "await_route" or not state["understanding_approved"]:
            raise WorkflowError("Маршрут можно заменить только до его утверждения.")
        state["route"] = self._normalize_proposal(proposal)
        self._gate(state, "route", state["route"])
        self.store.save(state, "route_replaced_unapproved")
        artifacts.save_route(Path(state["project"]), run_id, state["route"])
        return state

    def _context(self, state: dict, mod: dict, role: str, feedback: str) -> dict:
        approved = []
        for ref in mod["inputs"]:
            source = state["results"].get(ref, {})
            if not source.get("approved"):
                raise WorkflowError(f"Вход модуля {mod['id']} не утверждён: {ref}")
            path = f".workflow/runs/{state['id']}/modules/{ref}/handoff.md"
            approved.append({"stage": ref, "summary": shorten(source["report"]["summary"], 420),
                             "artifact": path})
        decisions = [row for row in state["decisions"] if row["module"] in set(mod["inputs"])]
        scoped_path = artifacts.save_scoped_decisions(Path(state["project"]), state["id"], mod["id"], decisions)
        stage = {"id": mod["id"], "template": mod["template"], "block": mod["category"],
                 "agents": mod["agents"], "goal": mod["goal"], "inputs": mod["inputs"]}
        if mod["template"] == "system_design":
            stage["system_design_mode"] = mod.get("design_mode", "light")
        ctx = {"run_id": state["id"], "task": state["understanding"], "stage": stage,
               "role": role, "approved_decisions": decisions[-20:],
               "all_decisions_artifact": scoped_path,
               "prior_approved_handoffs": approved, "feedback_to_this_agent": feedback,
               "workspace": state["project"], "system_design_mode": mod.get("design_mode", "none"),
               "scope_rule": "Only this module. No direct calls to other modules. New decisions go to the human gate."}
        if mod["template"] == "system_design":
            from .system_design import sections_for
            mode = mod["design_mode"]
            ctx.update(design_mode=mode, design_required_sections=list(sections_for(mode)))
        designs = [ref for ref in mod["inputs"] if next(
            (m["template"] for m in state["route"]["modules"] if m["id"] == ref), "") == "system_design"]
        if designs:
            paths = [f".workflow/runs/{state['id']}/modules/{ref}/system-design.md" for ref in designs]
            ctx.update(approved_system_design_artifact=paths[-1],
                       approved_system_design_artifacts=paths, approved_system_design_mode=state["route"]["system_design_mode"])
        if role == "documentation":
            ctx["git_scope"] = git_scope(Path(state["project"]), state["git"])
            ctx["documentation_rule"] = "Audit all docs; change only affected areas, not merely the last Git commit."
            ctx["docs_handoffs"] = [
                {"module": entry["id"], "agent": agent, "docs_handoff": shorten(out["docs_handoff"], 280)}
                for entry in state["route"]["modules"] if entry["id"] != mod["id"]
                for agent, out in state["results"].get(entry["id"], {}).get("agent_results", {}).items()
                if state["results"].get(entry["id"], {}).get("approved") and out.get("docs_handoff")
            ][:45]
            ctx["logged_agent_changes"] = sorted({
                p for entry in state["route"]["modules"] if entry["id"] != mod["id"]
                for out in state["results"].get(entry["id"], {}).get("agent_results", {}).values()
                if state["results"].get(entry["id"], {}).get("approved")
                for p in out.get("changed_files", [])
            })[:250]
        return ctx

    def _run(self, state: dict, mod: dict, *, allow_writes: bool, allow_shell: bool,
             unsafe_shell: bool) -> dict:
        entry = state["results"].setdefault(mod["id"], {"agent_results": {}, "pending_agents": list(mod["agents"]),
                                                            "feedback": {}, "approved": False, "report": None, "round": 0})
        entry["round"] += 1
        state["status"] = "running_module"
        state["current_module"] = mod["id"]
        state["running_owner"] = {"pid": os.getpid(), "hostname": socket.gethostname(),
                                  "start_ticks": self._start_ticks(os.getpid())}
        self.store.save(state, "module_started", {"module": mod["id"], "round": entry["round"]})
        project = Path(state["project"])
        for agent in list(entry["pending_agents"]):
            context = self._context(state, mod, agent, entry["feedback"].get(agent, ""))
            context["runtime_unsafe_shell"] = unsafe_shell
            try:
                result = self.provider.run_agent(agent, context, self.registry, project=project,
                                                 allow_writes=allow_writes, allow_shell=allow_shell)
                if not isinstance(result, dict) or result.get("status") not in ("completed", "blocked", "needs_decision"):
                    raise WorkflowError("Агент вернул некорректный результат/status.")
            except Exception as exc:
                result = {"status": "blocked", "summary": "Ошибка агента: " + shorten(exc, 700),
                          "evidence": [], "decisions": [], "risks": [shorten(exc, 700)],
                          "mermaid": "flowchart TB\n    A[Ошибка агента] --> B[Ручная проверка]",
                          "docs_handoff": "", "changed_files": [], "no_changes_needed": False,
                          "simulated": bool(self.provider.is_demo)}
            entry["agent_results"][agent] = result
            entry["pending_agents"].remove(agent)
            self.store.save(state, "agent_result_saved", {"module": mod["id"], "agent": agent,
                                                         "status": result["status"]})
        self._finish(state, mod)
        return self.load(state["id"])

    def _finish(self, state: dict, mod: dict) -> None:
        entry = state["results"][mod["id"]]
        results = entry["agent_results"]
        if set(results) != set(mod["agents"]):
            raise WorkflowError("Модуль не завершил всех назначенных агентов.")
        errors = CATALOG[mod["template"]].validate(results, design_mode=mod.get("design_mode", "none"))
        passed = not errors and all(out["status"] == "completed" for out in results.values())
        summary = "\n".join(f"• {role}: {shorten(out.get('summary', ''), 600)}" for role, out in results.items())
        if self.provider.is_demo:
            summary = "ДЕМОНСТРАЦИЯ — никакая работа не выполнялась.\n" + summary
        evidences: list[str] = []
        decisions: list[str] = []
        risks: list[str] = errors[:]
        for role, out in results.items():
            evidences.extend(f"{role}: {shorten(item, 350)}" for item in
                             (out.get("evidence", [])[:10] + out.get("tool_evidence", [])[:10]))
            decisions.extend(f"{role}: {shorten(item, 350)}" for item in out.get("decisions", [])[:12])
            risks.extend(f"{role}: {shorten(item, 350)}" for item in out.get("risks", [])[:12])
        mermaid = next((out.get("mermaid", "") for out in results.values()
                        if isinstance(out.get("mermaid"), str) and out["mermaid"].lstrip().startswith(
                            ("flowchart", "graph", "sequenceDiagram", "classDiagram", "erDiagram", "stateDiagram"))), "")
        if not mermaid:
            from .schema import stage_diagram
            mermaid = stage_diagram(mod)
        report = {"module": mod["id"], "status": "completed" if passed else "blocked",
                  "summary": summary, "evidence": evidences[:80], "decisions": decisions[:50],
                  "risks": risks[:50], "mermaid": mermaid,
                  "artifact": f".workflow/runs/{state['id']}/modules/{mod['id']}/handoff.md",
                  "simulated": self.provider.is_demo}
        # Brief AI review is optional. Failure cannot grant approvals, bypass checks or invent evidence.
        try:
            review = self.provider.review_step({"task": shorten(state["understanding"]["summary"], 750),
                                                "step": mod["id"], "reports": [{"agent": key,
                                                    "summary": shorten(value.get("summary"), 600),
                                                    "status": value["status"],
                                                    "evidence": value.get("evidence", [])[:12]}
                                                    for key, value in results.items()],
                                                "decisions": decisions[:25], "risks": risks[:25],
                                                "mermaid": mermaid}, self.registry)
            if isinstance(review, dict) and isinstance(review.get("summary"), str):
                report["orchestrator_summary"] = shorten(review["summary"], 1800)
                report["orchestrator_mermaid"] = review.get("mermaid", "")
        except Exception as exc:
            report["orchestrator_summary"] = "Недоступна AI-сводка; используйте исходные доказательства."
            report["review_warning"] = shorten(exc, 350)
        entry["report"] = report
        state["status"] = "await_module_review"
        state["running_owner"] = None
        self._gate(state, "module", {"module": mod["id"], "report": report})
        self.store.save(state, "module_review_requested", {"module": mod["id"], "status": report["status"]})
        artifacts.save_module(Path(state["project"]), state["id"], mod, entry)

    def review(self, run_id: str) -> dict:
        state = self.load(run_id)
        if state["status"] != "await_module_review":
            raise WorkflowError("Нет готового результата на согласовании.")
        mod = self.current(state)
        assert mod is not None
        return {"module": mod, "report": state["results"][mod["id"]]["report"],
                "token": state["gate"]["token"]}

    def approve(self, run_id: str, token: str, note: str = "") -> dict:
        state = self.load(run_id)
        status = state["status"]
        if status == "await_understanding":
            self._verify(state, token, "understanding")
            if state["understanding"].get("questions"):
                raise WorkflowError("Нельзя подтвердить понимание с открытыми вопросами.")
            if state["understanding"].get("assumptions") and not note.strip():
                raise WorkflowError("Уточните или явно подтвердите допущения через --note.")
            state["understanding_approved"] = True
            state["status"] = "route_draft"
            action = "understanding_approved"
        elif status == "await_route":
            self._verify(state, token, "route")
            if not state["understanding_approved"]:
                raise WorkflowError("Нет утверждённого понимания.")
            state["route_approved"] = True
            state["status"] = "ready_module"
            action = "route_approved"
        elif status == "await_module_review":
            self._verify(state, token, "module")
            mod = self.current(state)
            if mod is None:
                raise WorkflowError("Нет текущего модуля.")
            entry = state["results"][mod["id"]]
            if entry["report"]["status"] != "completed":
                raise WorkflowError("Модуль заблокирован. Исправьте замечания через revise.")
            if entry["report"]["decisions"] and not note.strip():
                raise WorkflowError("Требуется явное подтверждение принятых решений через --note.")
            entry["approved"] = True
            entry["approval_note"] = note.strip()
            state["decisions"].append({"module": mod["id"], "note": note.strip() or "Принято"})
            artifacts.save_module(Path(state["project"]), state["id"], mod, entry)
            artifacts.save_decisions(Path(state["project"]), run_id, state["decisions"])
            action = "module_approved"
            if self.current(state) is None:
                if mod["template"] != "documentation":
                    raise WorkflowError("Нельзя завершить Workflow без документации.")
                state["status"] = "demo_complete" if state["provider"] == "demo" else "completed"
            else:
                state["status"] = "ready_module"
        elif status == "await_replan":
            self._verify(state, token, "route_change")
            return self._commit_replan(state, note)
        else:
            raise WorkflowError("Нет ожидающего согласования.")
        state["gate"] = None
        self.store.save(state, action, {"note": note.strip()})
        return state

    def revise(self, run_id: str, role: str, feedback: str) -> dict:
        state = self.load(run_id)
        if state["status"] != "await_module_review":
            raise WorkflowError("Исправление доступно только до утверждения текущего модуля.")
        mod = self.current(state)
        assert mod is not None
        if role not in mod["agents"] or not feedback.strip():
            raise WorkflowError("Укажите конкретного агента текущего модуля и замечание.")
        entry = state["results"][mod["id"]]
        entry["feedback"][role] = feedback.strip()
        entry["agent_results"].pop(role, None)
        entry["pending_agents"] = [role]
        entry["report"] = None
        state["gate"] = None
        state["status"] = "ready_module"
        self.store.save(state, "module_revision_requested", {"module": mod["id"], "role": role,
                                                            "feedback": shorten(feedback, 300)})
        return state

    def propose_replan(self, run_id: str, proposal: dict, reason: str) -> dict:
        state = self.load(run_id)
        if state["status"] in ("running_module", "await_replan", "completed", "demo_complete"):
            raise WorkflowError("Нельзя сейчас изменять маршрут.")
        if not state["route_approved"] or not reason.strip():
            raise WorkflowError("Нужен ранее утверждённый маршрут и объяснение изменения.")
        route = self._normalize_proposal(proposal)
        by_old = {m["id"]: m for m in state["route"]["modules"]}
        changed = {m["id"] for m in route["modules"]
                   if m["id"] not in by_old or module_signature(m) != module_signature(by_old[m["id"]])}
        changed |= set(by_old) - {m["id"] for m in route["modules"]}
        preserved: set[str] = set()
        invalidated: set[str] = set(changed)
        for mod in route["modules"]:
            ident = mod["id"]
            if ident in invalidated or any(ref in invalidated for ref in mod["inputs"]):
                invalidated.add(ident)
            elif state["results"].get(ident, {}).get("approved"):
                preserved.add(ident)
        invalidated_approved = [name for name in invalidated if state["results"].get(name, {}).get("approved")]
        state["pending_replan"] = {"route": route, "reason": reason.strip(),
                                   "invalidated": sorted(invalidated),
                                   "approved_to_reopen": sorted(invalidated_approved),
                                   "retained_approved": sorted(preserved),
                                   "warning": "Не откатывает Git-файлы. Пересмотрите влияние на связанные результаты."}
        state["status"] = "await_replan"
        self._gate(state, "route_change", state["pending_replan"])
        self.store.save(state, "replan_proposed", {"invalidated": sorted(invalidated)})
        return state

    def _commit_replan(self, state: dict, note: str) -> dict:
        pending = state["pending_replan"]
        assert pending
        project = Path(state["project"])
        route = pending["route"]
        keep = set(pending["retained_approved"])
        for mod_id, old_entry in list(state["results"].items()):
            if old_entry.get("approved") and mod_id not in keep:
                artifacts.supersede(project, state["id"], mod_id)
        state["results"] = {ident: row for ident, row in state["results"].items() if ident in keep}
        state["decisions"] = [d for d in state["decisions"] if d["module"] in keep]
        state["route"] = route
        state["route_approved"] = True
        state["pending_replan"] = None
        state["gate"] = None
        state["status"] = "ready_module"
        self.store.save(state, "replan_approved", {"retained": sorted(keep), "note": note})
        artifacts.save_route(project, state["id"], route)
        artifacts.save_decisions(project, state["id"], state["decisions"])
        return state

    @staticmethod
    def _start_ticks(pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        except (OSError, IndexError):
            return None

    def recover(self, run_id: str, *, confirmed: bool) -> dict:
        state = self.load(run_id)
        if state["status"] != "running_module" or not confirmed:
            raise WorkflowError("Recover требует прерванного запуска и --confirm-inspected после проверки файлов/чатов.")
        owner = state.get("running_owner") or {}
        pid = owner.get("pid")
        if pid and owner.get("hostname") == socket.gethostname():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                raise WorkflowError("Нельзя проверить владелец процесса — остановите запуск вручную.")
            else:
                prior = owner.get("start_ticks")
                now = self._start_ticks(pid)
                if not (prior is not None and now is not None and now != prior):
                    raise WorkflowError("Исходный процесс может работать; recover запрещён до его завершения.")
        state["status"] = "ready_module"
        state["running_owner"] = None
        self.store.save(state, "manual_recovery_after_inspection")
        return state
