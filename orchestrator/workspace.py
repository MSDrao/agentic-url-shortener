"""Isolated working copy of the target codebase with snapshots and rollback.

Agents never touch the real repository: they read from the workspace and
propose changes; the engine commits changes here after gates pass. Snapshots
are full copies (cheap at this scale; a git worktree is the scale-up path).
"""

from __future__ import annotations

import difflib
import fnmatch
import shutil
from pathlib import Path

IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", "*.db", "*.db-*", ".coverage", "coverage.json", "junit.xml"
)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".ini", ".toml", ".yaml", ".yml", ".json", ".cfg"}


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, root: Path, snapshots: Path):
        self.root = Path(root)
        self.snapshots = Path(snapshots)

    @classmethod
    def create(cls, run_dir: Path, baseline: Path | None) -> "Workspace":
        root = run_dir / "workspace"
        snaps = run_dir / "snapshots"
        if baseline is not None:
            shutil.copytree(baseline, root, ignore=IGNORE)
        else:
            root.mkdir(parents=True)
        ws = cls(root, snaps)
        ws.snapshot("baseline")
        return ws

    # --- reading --------------------------------------------------------
    def _safe(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise WorkspaceError(f"path escapes workspace: {rel}")
        return p

    def read(self, rel: str) -> str | None:
        p = self._safe(rel)
        return p.read_text() if p.is_file() else None

    def files(self, pattern: str = "*") -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts:
                rel = p.relative_to(self.root).as_posix()
                if fnmatch.fnmatch(rel, pattern) and p.suffix in TEXT_SUFFIXES:
                    out.append(rel)
        return out

    # --- writing (engine only) -----------------------------------------
    def apply(self, changes: dict[str, str | None]) -> None:
        for rel, content in changes.items():
            p = self._safe(rel)
            if content is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)

    # --- snapshots ------------------------------------------------------
    def snapshot(self, label: str) -> Path:
        dest = self.snapshots / label
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.root, dest, ignore=IGNORE)
        return dest

    def has_snapshot(self, label: str) -> bool:
        return (self.snapshots / label).exists()

    def restore(self, label: str) -> None:
        src = self.snapshots / label
        if not src.exists():
            raise WorkspaceError(f"no snapshot '{label}'")
        shutil.rmtree(self.root)
        shutil.copytree(src, self.root, ignore=IGNORE)

    # --- review ---------------------------------------------------------
    def diff(self, against: str = "baseline", pattern: str = "*") -> str:
        base = Workspace(self.snapshots / against, self.snapshots)
        paths = sorted(set(base.files(pattern)) | set(self.files(pattern)))
        chunks: list[str] = []
        for rel in paths:
            a = base.read(rel)
            b = self.read(rel)
            if a == b:
                continue
            chunks.extend(
                difflib.unified_diff(
                    (a or "").splitlines(keepends=True),
                    (b or "").splitlines(keepends=True),
                    fromfile=f"a/{rel}" if a is not None else "/dev/null",
                    tofile=f"b/{rel}" if b is not None else "/dev/null",
                )
            )
        return "".join(chunks)

    def changed_files(self, against: str = "baseline") -> list[str]:
        base = Workspace(self.snapshots / against, self.snapshots)
        paths = sorted(set(base.files()) | set(self.files()))
        return [p for p in paths if base.read(p) != self.read(p)]
