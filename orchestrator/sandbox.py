"""Execution sandbox for agent-generated code (tests, app import for OpenAPI).

Generated code is untrusted: it must not see the orchestrator's secrets (e.g.
ANTHROPIC_API_KEY), must not reach the network, and must not exhaust the host.

Backends (policy `sandbox.backend`):
  process - portable default. Allowlisted environment (secrets stripped), private
            HOME/TMPDIR, POSIX resource limits (CPU seconds, file size, open files,
            address space where the OS supports it), its own process group killed on
            timeout. On Linux, if unprivileged user namespaces are available, the
            process also runs in an empty network namespace (no network).
            It does NOT isolate the filesystem: code can still read files the
            orchestrator user can read. Use docker for that.
  docker  - container per execution: --network none, read-only root and workspace
            mount, non-root user, all capabilities dropped, no-new-privileges, memory/
            CPU/PID limits; only the output directory is writable. Build the image with
            `docker build -f Dockerfile.sandbox -t orchestrator-sandbox:py311 .`
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_PREFIX_ALLOWLIST = ("SHORTENER_", "COVERAGE_", "PYTEST_")


class SandboxError(RuntimeError):
    pass


@dataclass
class SandboxConfig:
    backend: str = "process"
    timeout_seconds: int = 300
    cpu_seconds: int = 300
    memory_mb: int = 2048
    file_size_mb: int = 128
    max_open_files: int = 256
    pids_limit: int = 128
    network: str = "deny"  # deny | allow
    docker_image: str = "orchestrator-sandbox:py311"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_policy(cls, section: dict[str, Any]) -> "SandboxConfig":
        known = {k: v for k, v in section.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        cfg.backend = os.environ.get("ORCH_SANDBOX", cfg.backend)
        return cfg


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    isolation: dict[str, Any]


def _filtered_env(extra_env: dict[str, str]) -> dict[str, str]:
    for k in extra_env:
        if not k.startswith(ENV_PREFIX_ALLOWLIST) and k not in ("PYTHONPATH",):
            raise SandboxError(f"environment variable '{k}' is not allowlisted for sandboxed code")
    return dict(extra_env)


def _netns_prefix() -> list[str]:
    """Empty network namespace via unprivileged user namespaces (Linux), if available."""
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return []
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "true"], capture_output=True)
    return ["unshare", "--user", "--map-root-user", "--net"] if probe.returncode == 0 else []


def _limits(cfg: SandboxConfig):
    def apply() -> None:  # runs in the child between fork and exec
        import resource

        def setl(res, value):
            try:
                resource.setrlimit(res, (value, value))
            except (ValueError, OSError):
                pass  # not enforceable on this OS (e.g. RLIMIT_AS on macOS)

        setl(resource.RLIMIT_CPU, cfg.cpu_seconds)
        setl(resource.RLIMIT_FSIZE, cfg.file_size_mb * 1024 * 1024)
        setl(resource.RLIMIT_NOFILE, cfg.max_open_files)
        setl(resource.RLIMIT_AS, cfg.memory_mb * 1024 * 1024)
        setl(resource.RLIMIT_CORE, 0)
    return apply


def run(cmd: list[str], *, workspace: Path, out_dir: Path, cfg: SandboxConfig,
        extra_env: dict[str, str] | None = None) -> SandboxResult:
    """Run `cmd` (argv; argv[0] may be sys.executable) with cwd=workspace. Paths inside
    `cmd`/`extra_env` that point into workspace/out_dir are translated for docker."""
    workspace, out_dir = Path(workspace).resolve(), Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    env_extra = _filtered_env(extra_env or {})
    if cfg.backend == "docker":
        return _run_docker(cmd, workspace, out_dir, cfg, env_extra)
    if cfg.backend != "process":
        raise SandboxError(f"unknown sandbox backend '{cfg.backend}'")

    home = out_dir / "home"
    tmp = out_dir / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(workspace),
        **env_extra,
    }
    prefix = _netns_prefix() if cfg.network == "deny" else []
    isolation = {"backend": "process", "env": "allowlist", "rlimits": True,
                 "network": "none (netns)" if prefix else ("allowed" if cfg.network == "allow" else "not isolated on this OS"),
                 "filesystem": "not isolated (use docker backend)"}
    proc = subprocess.Popen(prefix + cmd, cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True, preexec_fn=_limits(cfg))
    try:
        out, err = proc.communicate(timeout=cfg.timeout_seconds)
        return SandboxResult(proc.returncode, out, err, False, isolation)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole process group, not just the parent
        out, err = proc.communicate()
        return SandboxResult(-9, out, err, True, isolation)


def _run_docker(cmd: list[str], workspace: Path, out_dir: Path, cfg: SandboxConfig,
                env_extra: dict[str, str]) -> SandboxResult:
    if not shutil.which("docker"):
        raise SandboxError("sandbox backend 'docker' selected but docker is not installed")
    os.chmod(out_dir, 0o777)  # writable by the unprivileged container user

    def tr(value: str) -> str:
        return value.replace(str(workspace), "/work").replace(str(out_dir), "/out")

    argv = ["python" if a == sys.executable else tr(a) for a in cmd]
    env = {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/work",
           **{k: tr(v) for k, v in env_extra.items()}}
    # A unique name lets us force-remove the container: killing the docker CLI client
    # does NOT stop the container it started.
    name = f"orch-sbx-{uuid.uuid4().hex[:12]}"
    docker = [
        "docker", "run", "--rm", "--init", "--name", name,
        "--stop-timeout", "1",
        "--network", "none" if cfg.network == "deny" else "bridge",
        "--read-only", "--tmpfs", "/tmp:rw,size=128m,mode=1777",
        "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", f"{cfg.memory_mb}m", "--cpus", "1", "--pids-limit", str(cfg.pids_limit),
        "-v", f"{workspace}:/work:ro", "-v", f"{out_dir}:/out:rw", "-w", "/work",
    ]
    for k, v in env.items():
        docker += ["-e", f"{k}={v}"]
    docker += [cfg.docker_image, *argv]
    isolation = {"backend": "docker", "image": cfg.docker_image, "network": "none" if cfg.network == "deny" else "bridge",
                 "filesystem": "read-only root + read-only workspace; /out writable", "user": "65534",
                 "container": name}
    try:
        p = subprocess.run(docker, capture_output=True, text=True, timeout=cfg.timeout_seconds)
    except subprocess.TimeoutExpired:
        _force_remove(name)
        return SandboxResult(-9, "", "container timed out and was force-removed", True, isolation)
    except BaseException:  # e.g. KeyboardInterrupt / cancellation: never leave the container running
        _force_remove(name)
        raise
    if p.returncode == 125:  # docker itself failed (image missing, daemon down, ...)
        raise SandboxError(f"docker failed to start the sandbox: {p.stderr.strip()[:400]}")
    return SandboxResult(p.returncode, p.stdout, p.stderr, False, isolation)


def _force_remove(name: str) -> None:
    """Kill and remove a sandbox container by name (idempotent; ignores 'no such container')."""
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass
