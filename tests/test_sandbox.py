"""The sandbox must keep generated code away from secrets, network and unbounded resources."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

from orchestrator import sandbox
from orchestrator.sandbox import SandboxConfig, SandboxError


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _docker_image_available(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


BACKENDS = ["process"] + (["docker"] if _docker_image_available(SandboxConfig().docker_image) else [])


@pytest.fixture(params=BACKENDS)
def cfg(request) -> SandboxConfig:
    return SandboxConfig(backend=request.param, timeout_seconds=20, cpu_seconds=20)


def test_secrets_are_not_inherited(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-secret")
    r = sandbox.run(_py("import os; print(sorted(os.environ))"), workspace=tmp_path, out_dir=tmp_path / "out", cfg=cfg)
    assert r.returncode == 0, r.stderr
    assert "ANTHROPIC_API_KEY" not in r.stdout and "AWS_SECRET_ACCESS_KEY" not in r.stdout


def test_only_allowlisted_env_can_be_passed(tmp_path):
    with pytest.raises(SandboxError):
        sandbox.run(_py("pass"), workspace=tmp_path, out_dir=tmp_path / "o", cfg=SandboxConfig(),
                    extra_env={"ANTHROPIC_API_KEY": "x"})


def test_network_is_denied(cfg, tmp_path):
    if cfg.backend == "process" and not sandbox._netns_prefix():
        pytest.skip("no unprivileged network namespaces on this OS (use the docker backend)")
    code = ("import socket\n"
            "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=3); print('CONNECTED')\n"
            "except OSError as e:\n    print('BLOCKED', e)")
    r = sandbox.run(_py(code), workspace=tmp_path, out_dir=tmp_path / "out", cfg=cfg)
    assert "BLOCKED" in r.stdout, r.stdout + r.stderr


def test_timeout_kills_the_whole_process_group(tmp_path):
    cfg = SandboxConfig(timeout_seconds=1)
    marker = tmp_path / "child-survived"
    code = ("import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3); open(r\"{marker}\", \"w\")'])\n"
            "time.sleep(60)")
    started = time.time()
    r = sandbox.run(_py(code), workspace=tmp_path, out_dir=tmp_path / "out", cfg=cfg)
    assert r.timed_out and time.time() - started < 10
    time.sleep(3.5)
    assert not marker.exists(), "grandchild process outlived the sandbox"


def test_file_size_limit(tmp_path):
    cfg = SandboxConfig(file_size_mb=1)
    code = "open('big.bin', 'wb').write(b'0' * 5 * 1024 * 1024)"
    r = sandbox.run(_py(code), workspace=tmp_path, out_dir=tmp_path / "out", cfg=cfg)
    assert r.returncode != 0


@pytest.mark.skipif("docker" not in BACKENDS, reason="docker sandbox image not built")
def test_docker_workspace_is_read_only(tmp_path):
    cfg = SandboxConfig(backend="docker", timeout_seconds=30)
    (tmp_path / "app.py").write_text("x = 1\n")
    code = ("import pathlib\n"
            "try:\n    pathlib.Path('app.py').write_text('pwned'); print('WROTE')\n"
            "except OSError:\n    print('READ-ONLY')\n"
            "pathlib.Path('/out/ok.txt').write_text('fine')")
    r = sandbox.run(_py(code), workspace=tmp_path, out_dir=tmp_path / "out", cfg=cfg)
    assert "READ-ONLY" in r.stdout, r.stdout + r.stderr
    assert (tmp_path / "app.py").read_text() == "x = 1\n"
    assert (tmp_path / "out" / "ok.txt").read_text() == "fine"


def test_docker_timeout_force_removes_the_container_mocked(tmp_path, monkeypatch):
    """Killing the docker CLI does not stop the container; the sandbox must `docker rm -f` it."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    r = sandbox.run(_py("pass"), workspace=tmp_path, out_dir=tmp_path / "out",
                    cfg=SandboxConfig(backend="docker", timeout_seconds=1))
    assert r.timed_out
    name = calls[0][calls[0].index("--name") + 1]
    assert ["docker", "rm", "-f", name] in calls


@pytest.mark.skipif("docker" not in BACKENDS, reason="docker sandbox image not built")
def test_docker_timeout_leaves_no_container_running(tmp_path):
    cfg = SandboxConfig(backend="docker", timeout_seconds=3)
    r = sandbox.run(_py("import time; time.sleep(120)"), workspace=tmp_path, out_dir=tmp_path / "out", cfg=cfg)
    assert r.timed_out
    name = r.isolation["container"]
    ps = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"name={name}"], capture_output=True, text=True)
    assert ps.stdout.strip() == "", f"container {name} still exists"
