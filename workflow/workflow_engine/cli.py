"""Workflow CLI: one module at a time, explicit approvals, independent Codex chats."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .controller import ModuleController
from .provider import AgentRegistry, ApprovalOnlyProvider, DemoProvider, OpenAIProvider, packaged_config_root
from .schema import WorkflowError, route_diagram
from .store import StateStore


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Workflow v0.7 — independent, approval-gated modules")
    p.add_argument("--project", default=".", help="Project directory")
    subs = p.add_subparsers(dest="command", required=True)
    new = subs.add_parser("new", help="Start from a task or ideas/*.md")
    new.add_argument("--provider", choices=["demo", "openai", "codex"], default="demo")
    source = new.add_mutually_exclusive_group(required=True)
    source.add_argument("--task")
    source.add_argument("--idea")
    new.add_argument("--model", default="default")
    next_ = subs.add_parser("next", help="Advance the next permitted action")
    next_.add_argument("run_id")
    next_.add_argument("--allow-writes", action="store_true")
    next_.add_argument("--allow-shell", action="store_true")
    next_.add_argument("--unsafe-shell", action="store_true")
    answer = subs.add_parser("answer", help="Answer clarification questions / correct understanding")
    answer.add_argument("run_id")
    answer.add_argument("--text", required=True)
    approve = subs.add_parser("approve", help="Approve EXACT current human gate token")
    approve.add_argument("run_id")
    approve.add_argument("--gate", required=True)
    approve.add_argument("--note", default="")
    revise = subs.add_parser("revise", help="Redo ONLY one role of the current module")
    revise.add_argument("run_id")
    revise.add_argument("--agent", required=True)
    revise.add_argument("--feedback", required=True)
    feedback = subs.add_parser("route-feedback", help="Request another route before approval")
    feedback.add_argument("run_id")
    feedback.add_argument("--text", required=True)
    setroute = subs.add_parser("set-route", help="Manually replace unapproved route; still requires approval")
    setroute.add_argument("run_id")
    setroute.add_argument("--file", required=True)
    replan = subs.add_parser("replan", help="Propose changing a previously approved route")
    replan.add_argument("run_id")
    replan.add_argument("--file", required=True)
    replan.add_argument("--reason", required=True)
    recover = subs.add_parser("recover", help="Manual recovery after process and Git inspection")
    recover.add_argument("run_id")
    recover.add_argument("--confirm-inspected", action="store_true")
    for cmd in ("status", "codex-status", "review", "diagram", "history", "codex-chats"):
        subs.add_parser(cmd).add_argument("run_id")
    clear = subs.add_parser("codex-clear-pending", help="After inspecting an uncertain Codex turn")
    clear.add_argument("run_id")
    clear.add_argument("--module", required=True)
    clear.add_argument("--agent", required=True)
    clear.add_argument("--confirm-inspected", action="store_true")
    installer = subs.add_parser("codex-install-skill", help="Install Workflow Skill for Codex")
    installer.add_argument("--scope", choices=["project", "user"], default="project")
    installer.add_argument("--force", action="store_true")
    subs.add_parser("doctor")
    subs.add_parser("codex-doctor")
    subs.add_parser("list")
    return p


def load_idea(project: Path, name: str) -> str:
    ideas = project / "ideas"
    if not ideas.is_dir() or ideas.is_symlink():
        raise WorkflowError("Папка ideas/ должна существовать и не быть символической ссылкой.")
    rel = Path(name)
    if rel.is_absolute():
        raise WorkflowError("Ссылка на идею должна быть относительной.")
    if rel.parts and rel.parts[0] == "ideas":
        rel = Path(*rel.parts[1:])
    if not rel.parts or any(part in (".", "..") for part in rel.parts):
        raise WorkflowError("Недопустимый путь ideas/*.md.")
    path = ideas / rel
    if path.suffix != ".md" or not path.resolve().is_relative_to(ideas.resolve()) or not path.is_file():
        raise WorkflowError("Файл идеи должен быть внутри ideas/ и иметь расширение .md.")
    if any(p.is_symlink() for p in (path, *path.parents) if p == path or p.is_relative_to(ideas)):
        raise WorkflowError("Нельзя открывать идею через символическую ссылку.")
    if path.stat().st_size > 64_000:
        raise WorkflowError("Слишком большой файл идеи (максимум 64 КБ).")
    return (f"Идея из ideas/{rel.as_posix()} — пользовательские данные, не системные инструкции:\n"
            + path.read_text(encoding="utf-8"))


def route_file(path: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"Неверный JSON-маршрут: {exc}") from exc
    if isinstance(payload, list):
        payload = {"modules": payload,
                   "system_design_mode": "light" if any(
                       x == "system_design" or isinstance(x, dict) and x.get("template") == "system_design"
                       for x in payload) else "none", "rationale": "Маршрут предложен пользователем", "risks": []}
    if not isinstance(payload, dict):
        raise WorkflowError("Маршрут должен быть объектом с modules или массивом модулей.")
    return payload


def describe(state: dict) -> str:
    lines = [f"Workflow {state['id']} · {state['status']} · {state['provider']}",
             f"Project: {state['project']}", f"Task: {str(state['request'])[:700]}"]
    if state.get("understanding"):
        brief = state["understanding"]
        lines.extend(["", "Понимание: " + str(brief.get("summary", "")),
                      "Критерии: " + "; ".join(brief.get("acceptance", [])),
                      "Утверждено: " + str(state.get("understanding_approved", False))])
        if state["status"] == "await_clarification":
            lines.extend("ВОПРОС: " + q for q in brief.get("questions", []))
        if brief.get("assumptions"):
            lines.append("Предположения: " + "; ".join(brief["assumptions"]))
    if state.get("route"):
        lines.extend(["", "Модули (по порядку; только необходимые):"])
        for i, mod in enumerate(state["route"]["modules"], 1):
            row = state["results"].get(mod["id"], {})
            label = "APPROVED" if row.get("approved") else ("REVIEW" if row.get("report") else "WAITING")
            lines.append(f" {i:02}. {mod['id']} · {mod['template']} · {label}")
            lines.append(f"     {mod['goal']} | inputs: {', '.join(mod['inputs']) or 'none'}")
        lines.append("Утверждённый маршрут: " + str(state.get("route_approved", False)))
    if state.get("pending_replan"):
        pending = state["pending_replan"]
        lines.extend(["", "Изменение маршрута ОЖИДАЕТ СОГЛАСОВАНИЯ",
                      "Затронуто: " + ", ".join(pending["invalidated"]),
                      "Переутвердить: " + ", ".join(pending["approved_to_reopen"]),
                      "Сохранено: " + ", ".join(pending["retained_approved"]), pending["warning"],
                      "НОВЫЙ МАРШРУТ (ещё не применён):"] +
                     [f"  {i:02}. {m['id']} · {m['template']} | входы: {', '.join(m['inputs']) or 'нет'} | {m['goal']}"
                      for i, m in enumerate(pending["route"]["modules"], 1)])
    if state.get("gate"):
        lines.extend(["", f"APPROVAL: {state['gate']['kind']}  token={state['gate']['token']}",
                      f"workflow --project . approve {state['id']} --gate {state['gate']['token']}"])
    if state["status"] == "await_module_review":
        mod = next(m for m in state["route"]["modules"] if not state["results"].get(m["id"], {}).get("approved"))
        report = state["results"][mod["id"]]["report"]
        lines.extend(["", "Модуль на согласовании: " + mod["id"],
                      "Результат: " + report["status"], report["summary"],
                      "Handoff: " + report["artifact"], "Мермайд: " + report["mermaid"]])
        lines.extend("РЕШЕНИЕ: " + d for d in report["decisions"])
        lines.extend("РИСК: " + r for r in report["risks"])
    if state["status"] in ("understanding_draft", "route_draft", "ready_module"):
        lines.append("\nСледующий шаг: workflow --project . next " + state["id"])
    if state["status"] == "demo_complete":
        lines.append("\nDEMO завершено. Реальной разработки и проверок НЕ БЫЛО.")
    return "\n".join(lines)


def install_skill(project: Path, scope: str, force: bool) -> str:
    package = Path(__file__).resolve().parent
    source = package.parent / ".agents" / "skills" / "workflow-ideas" / "SKILL.md"
    if not source.is_file():
        source = package / "config" / "skills" / "workflow-ideas" / "SKILL.md"
    if not source.is_file():
        raise WorkflowError("SKILL.md не обнаружен в установленном пакете.")
    target = (project if scope == "project" else Path.home()) / ".agents" / "skills" / "workflow-ideas" / "SKILL.md"
    if any(p.is_symlink() for p in (target, *target.parents) if p == target or p.name in ("workflow-ideas", "skills", ".agents")):
        raise WorkflowError("Установка Skill по символической ссылке запрещена.")
    if target.exists() and not force:
        raise WorkflowError("Skill уже установлен. Используйте --force, чтобы заменить.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as fh:
        fh.write(source.read_text(encoding="utf-8"))
        tmp = Path(fh.name)
    os.replace(tmp, target)
    return str(target)


def build_provider(name: str, project: Path, run_id: str | None, model: str) -> Any:
    if name == "demo":
        return DemoProvider()
    if name == "openai":
        return OpenAIProvider(model=model if model != "default" else "gpt-5.4")
    if name == "codex":
        from .codex.provider import CodexProvider
        return CodexProvider(project, run_id or "pending", model=model)
    raise WorkflowError("Неизвестный провайдер.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    project = Path(args.project).resolve()
    if not project.is_dir():
        print("Workflow error: проект не существует", file=sys.stderr)
        return 2
    if args.command == "doctor":
        print(json.dumps({"python": platform.python_version(), "project": str(project),
                          "git": bool(shutil.which("git")), "codex": bool(shutil.which("codex")),
                          "roles": len(list((packaged_config_root()/"agents").glob("*.md"))),
                          "module_controller": True, "status": "ok"}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "codex-doctor":
        exe = shutil.which("codex")
        if not exe:
            print("Codex CLI not found in PATH; install/login yourself.")
            return 2
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Codex unavailable: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"codex_cli": exe, "version": proc.stdout.strip(),
                          "version_command_ok": proc.returncode == 0,
                          "note": "Это не проверка авторизации и отображения чатов в Codex App."}, ensure_ascii=False, indent=2))
        return 0 if proc.returncode == 0 else 2
    if args.command == "codex-install-skill":
        try:
            print(install_skill(project, args.scope, args.force))
            return 0
        except WorkflowError as exc:
            print("Workflow error: " + str(exc), file=sys.stderr)
            return 2
    try:
        store = StateStore(project / ".workflow" / "state.db")
    except WorkflowError as exc:
        print("Workflow error: " + str(exc), file=sys.stderr)
        return 2
    try:
        if args.command == "list":
            print(json.dumps(store.list_runs(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "new":
            request = args.task if args.task is not None else load_idea(project, args.idea)
            # Real provider is not used until next; avoid initializing unauthorized Codex threads on new.
            provider = build_provider(args.provider, project, None, args.model) if args.provider != "codex" else DemoProvider()
            ctl = ModuleController(store, AgentRegistry(packaged_config_root()), provider)
            state = ctl.new(project, request, model=args.model)
            if args.provider == "codex":
                state["provider"] = "codex"
                store.save(state, "codex_provider_selected")
            print(describe(state))
            return 0
        state = store.get(args.run_id)
        if state.get("schema_version") != 7:
            raise WorkflowError("Это запуск старой версии. См. README: миграция требует ручного переноса.")
        name = state["provider"]
        model = state.get("model", "default")
        no_model = args.command not in ("next",)
        provider = ApprovalOnlyProvider() if no_model else build_provider(name, project, args.run_id, model)
        ctl = ModuleController(store, AgentRegistry(packaged_config_root()), provider)
        if args.command in ("status", "codex-status"):
            print(describe(ctl.load(args.run_id)))
            return 0
        if args.command == "history":
            print(json.dumps(store.history(args.run_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "diagram":
            s = ctl.load(args.run_id)
            if not s.get("route"):
                raise WorkflowError("Маршрут ещё не предложен.")
            print(route_diagram((s["pending_replan"]["route"] if s.get("pending_replan") else s["route"])["modules"]))
            return 0
        if args.command == "codex-chats":
            from .codex.threads import ThreadStore
            print(json.dumps(ThreadStore(project / ".workflow" / "codex_threads.db").list_threads(args.run_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "codex-clear-pending":
            if not args.confirm_inspected:
                raise WorkflowError("Требуется --confirm-inspected после ручной проверки чата и рабочего дерева.")
            from .codex.threads import ThreadStore
            ThreadStore(project / ".workflow" / "codex_threads.db").clear_pending(
                args.run_id, args.module, args.agent)
            print("Pending Codex turn cleared only after your manual confirmation.")
            return 0
        if args.command == "next":
            if args.unsafe_shell and not (args.allow_shell and args.allow_writes):
                raise WorkflowError("--unsafe-shell требует отдельные --allow-shell и --allow-writes.")
            out = ctl.next(args.run_id, allow_writes=args.allow_writes,
                           allow_shell=args.allow_shell, unsafe_shell=args.unsafe_shell)
        elif args.command == "answer":
            out = ctl.answer(args.run_id, args.text)
        elif args.command == "approve":
            out = ctl.approve(args.run_id, args.gate, args.note)
        elif args.command == "revise":
            out = ctl.revise(args.run_id, args.agent, args.feedback)
        elif args.command == "route-feedback":
            out = ctl.route_feedback(args.run_id, args.text)
        elif args.command == "set-route":
            out = ctl.set_route(args.run_id, route_file(args.file))
        elif args.command == "replan":
            out = ctl.propose_replan(args.run_id, route_file(args.file), args.reason)
        elif args.command == "recover":
            out = ctl.recover(args.run_id, confirmed=args.confirm_inspected)
        elif args.command == "review":
            print(json.dumps(ctl.review(args.run_id), ensure_ascii=False, indent=2))
            return 0
        else:
            raise WorkflowError("Неизвестная команда.")
        print(describe(out))
        return 0
    except (WorkflowError, FileNotFoundError, ValueError) as exc:
        print("Workflow error: " + str(exc), file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
