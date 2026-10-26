"""Linux user+mount+PID+network namespace runner for allowlisted project commands.

Runs as an explicit child process. Fail closed if namespace or mounts unavailable.
Not a substitute for a VM/container/security audit. No project code is executed by this
module until the private filesystem view has been created and chrooted.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _mount(*args: str) -> None:
    proc = subprocess.run(["mount", *args], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode:
        raise RuntimeError(f"mount {args[0]} failed: {proc.stderr[:150]}")


def _bind(src: Path, dest: Path, readonly: bool = True) -> None:
    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.touch(exist_ok=True)
    _mount("--bind", str(src), str(dest))
    if readonly:
        _mount("-o", "remount,bind,ro", str(dest))


def launch(workspace: Path, mount_root: Path, command: list[str]) -> int:
    """Only the outer caller is trusted to provide a pre-vetted command."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Linux user namespace with mapped root is required")
    if not workspace.is_dir():
        raise RuntimeError("Workspace does not exist")
    # New mount namespace, never propagate mounts to the real host.
    _mount("--make-rprivate", "/")
    # Root mount point is created on the host, but its tmpfs contents exist only
    # in this private mount namespace. Parent cleans the empty directory.
    sandbox_root = mount_root
    _mount("-t", "tmpfs", "-o", "size=128m,nosuid,nodev", "tmpfs", str(sandbox_root))
    for source in ("/usr", "/usr/local", "/bin", "/lib", "/lib64"):
        src = Path(source)
        if src.exists():
            _bind(src, sandbox_root / src.relative_to("/"), readonly=True)
    # Minimal local config for dynamic linker, locale and name resolution.
    for source in ("/etc/ld.so.cache", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group",
                   "/etc/ssl/certs", "/etc/localtime"):
        src = Path(source)
        if src.exists():
            _bind(src, sandbox_root / src.relative_to("/"), readonly=True)
    for device in ("null", "zero", "urandom", "random"):
        _bind(Path("/dev") / device, sandbox_root / "dev" / device, readonly=False)
    _bind(workspace.resolve(), sandbox_root / "workspace", readonly=True)
    (sandbox_root / "tmp").mkdir()
    (sandbox_root / "proc").mkdir()
    _mount("-t", "tmpfs", "-o", "size=64m,nosuid,nodev", "tmpfs", str(sandbox_root / "tmp"))
    # Some restricted kernels prohibit a new procfs mount. An empty /proc is safer
    # than inheriting the host procfs; most allowlisted commands do not need it.
    os.chroot(sandbox_root)
    os.chdir("/workspace")
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (60, 61))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    # Network is isolated by `unshare --net`: no external sockets/routes.
    # Strip namespace-root capabilities before running UNTRUSTED test code.
    # In particular CAP_SYS_ADMIN would otherwise allow remounting the
    # read-only /workspace. setpriv is an explicit Linux sandbox prerequisite.
    secure = ["setpriv", "--no-new-privs", "--bounding-set=-all", "--inh-caps=-all",
              "--ambient-caps=-all", "--securebits=+noroot", *command]
    os.execvpe("setpriv", secure, os.environ)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--mount-root", required=True)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.cmd or args.cmd[0] != "--":
        print("Sandbox: expected command after --", file=sys.stderr)
        return 2
    try:
        return launch(Path(args.workspace), Path(args.mount_root), args.cmd[1:])
    except Exception as exc:
        print(f"Sandbox setup failed: {exc}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    sys.exit(main())
