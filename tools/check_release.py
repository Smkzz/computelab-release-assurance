"""Inspect source and distribution boundaries; not a full secret scanner."""

from __future__ import annotations

import ast
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "commercial",
    "prospects",
    "results",
    "reports",
    "sealed",
    "worktrees",
    ".freebuff",
    "node_modules",
}
FORBIDDEN_SUFFIX = {".sqlite", ".db", ".pem", ".key", ".safetensors", ".pt", ".pth", ".pkl"}


def check_name(name: str) -> None:
    p = Path(name)
    if (
        p.is_absolute()
        or ".." in p.parts
        or set(p.parts) & FORBIDDEN
        or p.suffix in FORBIDDEN_SUFFIX
    ):
        raise SystemExit(f"Forbidden release path: {name}")


def main() -> None:
    for path in (ROOT / "src").rglob("*"):
        if path.is_symlink():
            raise SystemExit("Source symlinks are not allowed")
        if path.is_file() and "__pycache__" not in path.parts:
            check_name(str(path.relative_to(ROOT)))
            if path.suffix == ".py":
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name.split(".")[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [(node.module or "").split(".")[0]]
                    else:
                        continue
                    if set(names) & {"torch", "vllm", "numpy", "psutil", "compute_discovery_lab"}:
                        raise SystemExit("Unexpected research dependency in public runtime")
    for artifact in (ROOT / "dist").glob("*"):
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                for name in archive.namelist():
                    check_name(name)
        elif artifact.name.endswith(".tar.gz"):
            with tarfile.open(artifact) as archive:
                for member in archive.getmembers():
                    check_name(member.name)
                    if member.issym() or member.islnk():
                        raise SystemExit("Archive links are not allowed")
    print("Source and distribution boundary checks passed")


if __name__ == "__main__":
    main()
