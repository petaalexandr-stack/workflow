"""Small, opt-in workspace tools for live agents. NOT an OS sandbox."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import signal
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .schema import WorkflowError

READABLE = {"read", "search"}
IGNORED_DIRS = {".git", ".workflow", "node_modules", ".venv", "venv", "__pycache__", ".idea"}
MAX_FILE_BYTES = 120_000
MAX_FILE_READ_BYTES = 2_000_000
MAX_TEXT_RESULT = 20_000
MAX_SCAN_FILES = 20_000


def parse_agent_tools(prompt: str) -> set[str]:
    """Parse only the explicit tools: [read, search, edit, shell] frontmatter key."""
    for line in prompt.splitlines()[:15]:
        line = line.strip()
        if line.startswith("tools:"):
            raw = line.removeprefix("tools:").strip().strip("[]")
            return {v.strip().strip("'\"") for v in raw.split(",") if v.strip()}
    return set()


class WorkspaceTools:
    def __init__(self, project: Path, role_tools: set[str], *, write_allowed: bool,
                 shell_allowed: bool, git_base_commit: str | None = None,
                 approved_artifacts: set[str] | None = None, unsafe_shell: bool = False):
        self.root = project.resolve()
        self.role_tools = role_tools
        self.write_allowed = write_allowed
        self.shell_allowed = shell_allowed
        self.unsafe_shell = unsafe_shell
        self.git_base_commit = git_base_commit
        self.approved_artifacts = approved_artifacts or set()
        self.evidence: list[str] = []
        self.changed_files: list[str] = []

    def _resolve(self, name: str, *, writing: bool = False) -> Path:
        if not name or "\x00" in name:
            raise WorkflowError("Пустой или недопустимый путь.")
        relative = Path(name)
        if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
            raise WorkflowError("Разрешены только относительные пути в проекте.")
        if any(part in IGNORED_DIRS for part in relative.parts):
            raise WorkflowError("Системный каталог недоступен агентам.")
        if any(part.startswith(".env") or part.endswith((".pem", ".key", ".p12")) for part in relative.parts):
            raise WorkflowError("Потенциальный секретный файл недоступен агентам.")
        # Never follow symlinks for writes; a symlink alias must not bypass the
        # protected config/.workflow directories or point outside the project.
        original = self.root / relative
        if writing and any(p.is_symlink() for p in (original, *original.parents) if p != self.root and p.is_relative_to(self.root)):
            raise WorkflowError("Запись через символьную ссылку запрещена.")
        resolved = original.resolve()
        if not resolved.is_relative_to(self.root):
            raise WorkflowError("Путь выходит за пределы проекта.")
        rel_resolved = resolved.relative_to(self.root)
        if writing and (rel_resolved.as_posix() == "AGENTS.md" or
                        rel_resolved.parts[0] in {"agents", ".workflow", ".git"}):
            raise WorkflowError("Агентам запрещено изменять конфигурацию и состояние Workflow.")
        return resolved

    def _allowed(self, tool: str) -> None:
        group = {"list_files": "search", "search_files": "search", "read_file": "read",
                 "write_file": "edit", "run_command": "shell", "read_git_diff": "search", "read_approved_artifact": "read"}[tool]
        if group not in self.role_tools:
            raise WorkflowError(f"Инструмент {tool} не разрешён ролью.")
        if tool == "read_approved_artifact" and not self.approved_artifacts:
            raise WorkflowError("Нет утверждённых артефактов для чтения.")
        if tool == "read_git_diff" and not self.git_base_commit:
            raise WorkflowError("Git diff недоступен без закреплённого base_commit.")
        if group == "edit" and not self.write_allowed:
            raise WorkflowError("Запись заблокирована: нужен запуск --allow-writes после утверждения этапа.")
        if group == "shell" and not self.shell_allowed:
            raise WorkflowError("Команды заблокированы: нужен запуск --allow-shell после утверждения этапа.")

    def list_files(self, directory: str = ".", offset: int = 0) -> dict[str, Any]:
        root = self._resolve(directory)
        if not root.is_dir():
            raise WorkflowError("Каталог не найден.")
        if not isinstance(offset, int) or offset < 0:
            raise WorkflowError("offset должен быть неотрицательным целым числом.")
        entries = []
        limited = False
        for parent, directories, filenames in os.walk(root):
            directories[:] = sorted(d for d in directories if d not in IGNORED_DIRS
                                     and not (Path(parent) / d).is_symlink())
            for name in sorted(filenames):
                path = Path(parent) / name
                if path.is_symlink():
                    continue
                relative = path.relative_to(self.root).as_posix()
                try:
                    self._resolve(relative)
                except WorkflowError:
                    continue
                entries.append(relative)
                if len(entries) >= MAX_SCAN_FILES:
                    limited = True
                    break
            if limited:
                break
        result = entries[offset:offset + 200]
        next_offset = offset + len(result)
        self.evidence.append("list_files: " + directory)
        return {"files": result, "total": len(entries), "truncated": limited,
                "note": "Сузьте directory для полного списка." if limited else "",
                "next_offset": next_offset if next_offset < len(entries) else None}

    def read_file(self, path: str, offset: int = 0) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise WorkflowError("Файл не найден.")
        if not isinstance(offset, int) or offset < 0:
            raise WorkflowError("offset должен быть неотрицательным целым числом.")
        if target.stat().st_size > MAX_FILE_READ_BYTES:
            raise WorkflowError("Файл превышает 2MB; используйте специализированные инструменты.")
        raw = target.read_bytes()
        data = raw.decode("utf-8")
        chunk = data[offset:offset + MAX_TEXT_RESULT]
        next_offset = offset + len(chunk)
        self.evidence.append("read_file: " + path)
        return {"path": path, "content": chunk,
                "next_offset": next_offset if next_offset < len(data) else None,
                "sha256": hashlib.sha256(raw).hexdigest()}

    def read_approved_artifact(self, path: str) -> dict[str, Any]:
        if path not in self.approved_artifacts:
            raise WorkflowError("Артефакт не является разрешённым входом этого агента.")
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            raise WorkflowError("Разрешённый артефакт отсутствует.")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise WorkflowError("Артефакт слишком велик.")
        content = target.read_text(encoding="utf-8")
        self.evidence.append("read_approved_artifact: " + path)
        return {"path": path, "content": content[:MAX_TEXT_RESULT],
                "truncated": len(content) > MAX_TEXT_RESULT}

    def search_files(self, query: str) -> dict[str, Any]:
        if not query or len(query) > 200:
            raise WorkflowError("Строка поиска должна иметь длину 1–200 символов.")
        self.evidence.append("search_files: " + query[:60])
        results = []
        scanned = 0
        for parent, directories, filenames in os.walk(self.root):
            directories[:] = [d for d in directories if d not in IGNORED_DIRS
                              and not (Path(parent) / d).is_symlink()]
            for filename in filenames:
                file = Path(parent) / filename
                if file.is_symlink():
                    continue
                scanned += 1
                if scanned > MAX_SCAN_FILES:
                    return {"matches": results, "truncated": True,
                            "note": "Превышен лимит сканирования; уточните запрос или каталог."}
                relative = file.relative_to(self.root).as_posix()
                try:
                    self._resolve(relative)
                except WorkflowError:
                    continue
                if file.stat().st_size > MAX_FILE_BYTES:
                    continue
                try:
                    for line_no, text in enumerate(file.read_text("utf-8").splitlines(), 1):
                        if query.casefold() in text.casefold():
                            results.append({"path": relative, "line": line_no, "text": text[:250]})
                            if len(results) == 60:
                                return {"matches": results, "truncated": True}
                except (UnicodeError, OSError):
                    continue
        return {"matches": results, "truncated": False}

    def read_git_diff(self, path: str) -> dict[str, Any]:
        if not self.git_base_commit or len(self.git_base_commit) > 64 or not all(
            char in "0123456789abcdef" for char in self.git_base_commit.lower()
        ):
            raise WorkflowError("Нет подтверждённого Git base_commit для этой задачи.")
        target = self._resolve(path)
        relative = target.relative_to(self.root).as_posix()
        prefix = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
        commands = [prefix + ["diff", "--no-ext-diff", "--no-textconv", self.git_base_commit, "HEAD", "--", relative],
                    prefix + ["diff", "--no-ext-diff", "--no-textconv", "--", relative],
                    prefix + ["diff", "--no-ext-diff", "--no-textconv", "--cached", "--", relative]]
        patches = []
        for argv in commands:
            process = subprocess.run(argv, cwd=self.root, capture_output=True, text=True,
                                     check=False, timeout=15)
            if process.returncode not in (0, 1):
                raise WorkflowError("Git diff не может быть прочитан.")
            if process.stdout:
                patches.append(process.stdout)
        self.evidence.append("read_git_diff: " + relative)
        return {"path": relative, "diff": "\n".join(patches)[:MAX_TEXT_RESULT],
                "truncated": sum(map(len, patches)) > MAX_TEXT_RESULT}

    def write_file(self, path: str, content: str, expected_sha256: str) -> dict[str, Any]:
        target = self._resolve(path, writing=True)
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise WorkflowError("Файл превышает разрешённый размер.")
        if target.exists() and not target.is_file():
            raise WorkflowError("Путь не является обычным файлом.")
        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else ""
        if expected_sha256 != actual:
            raise WorkflowError("Конфликт изменения: текущая версия файла отличается от прочитанной.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again after mkdir, to prevent simple symlink path escaping.
        if not target.resolve().is_relative_to(self.root):
            raise WorkflowError("Путь выходит за пределы проекта.")
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".workflow-writing-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        relative = target.relative_to(self.root).as_posix()
        self.changed_files.append(relative)
        self.evidence.append("write_file: " + relative)
        return {"written": relative, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}

    def run_command(self, command: str) -> dict[str, Any]:
        """Run a fixed allowlist. Linux: private FS/network/PID namespaces.

        Without Linux user namespaces this fails closed unless the operator
        explicitly opted into --unsafe-shell for a trusted repository.
        """
        command_map = {
            "git status --short": ["git", "status", "--short"],
            "git diff --stat": ["git", "diff", "--stat"],
            "python -m unittest discover": ["python", "-m", "unittest", "discover"],
            "python -m pytest": ["python", "-m", "pytest"],
        }
        if command not in command_map:
            raise WorkflowError("Команда отсутствует в разрешённом списке.")
        argv = command_map[command]
        env = {key: os.environ[key] for key in ("PATH", "LANG", "PYTHONPATH") if key in os.environ and key != "PYTHONPATH"}
        env.update({"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1"})
        # Git may invoke external fsmonitor/pager hooks: disable them explicitly.
        if argv[0] == "git":
            argv = ["git", "-c", "core.fsmonitor=false", "-c", "core.pager=cat", *argv[1:]]
        sandboxed = False
        temp_mount = tempfile.TemporaryDirectory(prefix="wf-sbox-root-")
        if not self.unsafe_shell:
            if sys.platform != "linux" or not shutil.which("unshare"):
                raise WorkflowError("OS sandbox недоступен; команда запрещена. Для доверенного проекта есть --unsafe-shell.")
            argv = ["unshare", "--user", "--map-root-user", "--mount", "--pid", "--net", "--fork",
                    sys.executable, str(Path(__file__).with_name("sandbox.py")),
                    "--workspace", str(self.root), "--mount-root", temp_mount.name,
                    "--", *argv]
            sandboxed = True
        try:
            with temp_mount:
                process = subprocess.Popen(argv, cwd=self.root, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, env=env,
                                           start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=65)
                except subprocess.TimeoutExpired as exc:
                    # Kill the WHOLE process group, not just the unshare wrapper:
                    # tests can spawn children, and they must not survive timeout.
                    if hasattr(os, "killpg"):
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.communicate()
                    raise WorkflowError("Команда превысила лимит 65 секунд; все дочерние процессы остановлены.") from exc
        except OSError as exc:
            raise WorkflowError(f"Не удалось выполнить команду: {exc}") from exc
        if sandboxed and process.returncode in (125, 126, 127) and "Sandbox setup failed" in stderr:
            raise WorkflowError("Песочница не запустилась: " + stderr[:300])
        if sandboxed and process.returncode != 0 and "unshare: " in stderr:
            raise WorkflowError("Изоляция Linux недоступна: " + stderr[:300])
        self.evidence.append(f"command: {command}, exit={process.returncode}, sandboxed={sandboxed}")
        return {"command": command, "exit_code": process.returncode,
                "sandboxed": sandboxed, "stdout": stdout[:MAX_TEXT_RESULT],
                "stderr": stderr[:3000]}

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            self._allowed(name)
            result = getattr(self, name)(**arguments)
            return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        except (WorkflowError, OSError, UnicodeError, TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def definitions(self) -> list[dict[str, Any]]:
        # JSON schemas are deliberately short and strict.
        items = {
            "read_file": ("Read UTF-8 file in 20K character pages; start with offset=0", {"path": {"type": "string"}, "offset": {"type": "integer"}}),
            "list_files": ("List up to 200 files, with offset pagination", {"directory": {"type": "string"}, "offset": {"type": "integer"}}),
            "search_files": ("Search files for a literal phrase", {"query": {"type": "string"}}),
            "read_git_diff": ("Read task-scoped Git diff for a specific file, if base commit exists", {"path": {"type": "string"}}),
            "read_approved_artifact": ("Read a whitelisted, approved handoff or decision log", {"path": {"type": "string"}}),
            "write_file": ("Write full UTF-8 file with optimistic hash check; read it first", {
                "path": {"type": "string"}, "content": {"type": "string"}, "expected_sha256": {"type": "string"}}),
            "run_command": ("Run exact allowlisted command: git status --short; git diff --stat; python -m unittest discover; python -m pytest", {"command": {"type": "string"}}),
        }
        allowed = []
        for name, (description, props) in items.items():
            try:
                self._allowed(name)
            except WorkflowError:
                continue
            allowed.append({"type": "function", "name": name, "description": description,
                            "strict": True,
                            "parameters": {"type": "object", "properties": props,
                                           "required": list(props), "additionalProperties": False}})
        return allowed
