"""Small, dependency-free Codex app-server JSONL client.

Each invocation owns its child process. Persistent Codex threads are stored by
Codex, not by the child process; a new process resumes by thread id.
"""
from __future__ import annotations

from collections import deque
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..schema import WorkflowError


def split_codex_command(raw: str, *, windows: bool | None = None) -> list[str]:
    """Parse a configured executable path without dropping Windows backslashes."""
    is_windows = os.name == "nt" if windows is None else windows
    args = shlex.split(raw, posix=not is_windows)
    if is_windows:
        # shlex in non-POSIX mode retains balanced surrounding quotes.
        args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in ("\"", "'") else a
                for a in args]
    return args


class CodexAppServer:
    def __init__(self, project: Path, *, command: str | None = None, timeout: int = 600):
        self.project = Path(project).resolve()
        self.command = split_codex_command(command or os.environ.get("WORKFLOW_CODEX_COMMAND", "codex"))
        if not self.command:
            raise WorkflowError("Не задана команда Codex.")
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self.inbox: queue.Queue[Any] = queue.Queue()
        self.events: deque[dict[str, Any]] = deque()
        self.errors: deque[str] = deque(maxlen=30)
        self.seq = 0
        self._write_lock = threading.Lock()

    def __enter__(self) -> "CodexAppServer":
        try:
            self.process = subprocess.Popen(
                [*self.command, "app-server", "--listen", "stdio://"],
                cwd=self.project, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                bufsize=1, env=os.environ.copy(),
            )
        except OSError as exc:
            raise WorkflowError(f"Codex CLI не запускается: {exc}") from exc
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {
                "name": "workflow_bridge", "title": "Workflow Codex Bridge", "version": "0.7.0"}}, timeout=25)
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise
        return self

    def _reader(self) -> None:
        assert self.process and self.process.stdout
        for raw in self.process.stdout:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    self.inbox.put(obj)
            except ValueError:
                self.errors.append(raw.strip()[:350])
        self.inbox.put(None)  # EOF sentinel

    def _stderr(self) -> None:
        assert self.process and self.process.stderr
        for raw in self.process.stderr:
            self.errors.append(raw.strip()[:500])

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        if self.process.poll() is not None:
            raise WorkflowError(f"Codex app-server завершился ({self.process.returncode}).")
        with self._write_lock:
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise WorkflowError(f"Обрыв соединения с Codex: {exc}") from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _event(self, obj: dict[str, Any]) -> None:
        if "id" in obj and "method" in obj:  # incoming server request: never silently approve
            method = obj["method"]
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self._send({"id": obj["id"], "result": {"decision": "decline"}})
            elif method == "item/permissions/requestApproval":
                self._send({"id": obj["id"], "result": {"permissions": {}}})
            elif method == "mcpServer/elicitation/request":
                self._send({"id": obj["id"], "result": {"action": "decline", "content": None}})
            else:
                self._send({"id": obj["id"], "error": {"code": -32601, "message": "Workflow Bridge: request not allowed"}})
        elif "method" in obj:
            self.events.append(obj)

    def _next(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkflowError("Таймаут Codex app-server; перед повтором проверьте состояние чата.")
        try:
            message = self.inbox.get(timeout=remaining)
        except queue.Empty as exc:
            raise WorkflowError("Таймаут Codex app-server; повторный запуск может дублировать ход.") from exc
        if message is None:
            raise WorkflowError("Codex app-server закрыл канал: " + " | ".join(self.errors)[-500:])
        return message

    def request(self, method: str, params: dict[str, Any], *, timeout: int | None = None) -> Any:
        self.seq += 1
        rid = self.seq
        self._send({"id": rid, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            msg = self._next(deadline)
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise WorkflowError(f"Codex {method}: {msg['error']}")
                return msg.get("result", {})
            self._event(msg)

    def new_thread(self, name: str, *, model: str = "default") -> str:
        params: dict[str, Any] = {"cwd": str(self.project)}
        if model and model != "default":
            params["model"] = model
        result = self.request("thread/start", params)
        thread_id = result.get("thread", {}).get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise WorkflowError("Codex thread/start не вернул thread.id")
        self.request("thread/name/set", {"threadId": thread_id, "name": name})
        return thread_id

    def resume(self, thread_id: str) -> None:
        result = self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        if result.get("thread", {}).get("id") != thread_id:
            raise WorkflowError("Codex thread/resume вернул неверный thread.id")

    def turn(self, thread_id: str, text: str, schema: dict[str, Any], *, write: bool = False,
             restricted_read: bool = False, model: str = "default") -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id, "input": [{"type": "text", "text": text}],
            "approvalPolicy": "never",
            "sandboxPolicy": ({"type": "workspaceWrite", "writableRoots": [str(self.project)], "networkAccess": False}
                              if write else ({"type": "readOnly", "access": {
                                  "type": "restricted", "includePlatformDefaults": True,
                                  "readableRoots": []}}
                                  if restricted_read else {"type": "readOnly"})),
            "outputSchema": schema,
        }
        if model and model != "default":
            params["model"] = model
        start = self.request("turn/start", params)
        turn_id = start.get("turn", {}).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise WorkflowError("Codex turn/start не вернул turn.id")
        final_text = ""
        tool_evidence: list[str] = []
        changed: list[str] = []
        deadline = time.monotonic() + self.timeout
        while True:
            msg = self.events.popleft() if self.events else self._next(deadline)
            if msg.get("id") is not None and "method" in msg:
                self._event(msg)
                continue
            if "method" not in msg:
                continue
            method = msg["method"]
            data = msg.get("params") or {}
            if data.get("threadId") not in (None, thread_id):
                continue
            if data.get("turnId") not in (None, turn_id):
                continue
            if method == "item/completed":
                item = data.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    # A turn may emit intermediate messages. The final one is authoritative.
                    final_text = item["text"]
                if item.get("type") == "commandExecution":
                    tool_evidence.append(f"commandExecution status={item.get('status')} exit={item.get('exitCode')}")
                if item.get("type") == "fileChange":
                    tool_evidence.append(f"fileChange status={item.get('status')}")
                    if item.get("status") == "completed":
                        for change in item.get("changes") or []:
                            if isinstance(change, dict) and isinstance(change.get("path"), str):
                                changed.append(change["path"])
            elif method == "turn/completed":
                turn = data.get("turn") or {}
                if turn.get("id") != turn_id:
                    continue
                if turn.get("status") != "completed":
                    raise WorkflowError(f"Codex turn {turn_id}: {turn.get('status')} {turn.get('error')}")
                if not final_text:
                    for item in turn.get("items", []):
                        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                            final_text = item["text"]
                if not final_text:
                    raise WorkflowError("Codex завершил ход без agentMessage JSON.")
                try:
                    result = json.loads(final_text)
                except ValueError as exc:
                    raise WorkflowError("Ответ Codex не соответствует JSON outputSchema: " + final_text[:200]) from exc
                if not isinstance(result, dict):
                    raise WorkflowError("Codex должен вернуть JSON-объект.")
                return {"result": result, "thread_id": thread_id, "turn_id": turn_id,
                        "tool_evidence": tool_evidence[:30], "file_change_events": changed[:100]}
            else:
                self._event(msg) if "id" in msg and "method" in msg else None

    def close(self) -> None:
        if not self.process:
            return
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            if stream and not stream.closed:
                stream.close()

    def __exit__(self, _typ: Any, _exc: Any, _tb: Any) -> None:
        self.close()
