from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "1.6.1"

SKIP_DIRS = {
    ".git", ".venv", "venv", "build", "dist", "release", "models", "logs",
    "__pycache__", ".pytest_cache", ".ruff_cache", "playwright-profile",
}
RUNTIME_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"}
FORBIDDEN_NAMES = {"loggg2.txt", "loggg2.prev.txt", ".env"}
MERGE_RE = re.compile(rb"(?m)^(?:<<<<<<<(?: .*)?|=======|>>>>>>>(?: .*)?)\r?$")


def tracked_candidate(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
        return False
    return path.is_file()


def fail(message: str) -> None:
    print(f"PRECHECK FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_pyproject() -> None:
    path = ROOT / "pyproject.toml"
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:  # pragma: no cover - exact parser message varies by Python
        fail(f"pyproject.toml is invalid TOML: {exc}")
    version = str(data.get("project", {}).get("version", "")).strip()
    if version != EXPECTED_VERSION:
        fail(f"pyproject version is {version!r}, expected {EXPECTED_VERSION!r}")
    if data.get("project", {}).get("name") != "eirven-ai":
        fail("pyproject project.name must be 'eirven-ai'")


def validate_tree() -> None:
    offenders: list[str] = []
    conflicts: list[str] = []
    for path in ROOT.rglob("*"):
        if not tracked_candidate(path):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if path.name in FORBIDDEN_NAMES or path.suffix.casefold() in RUNTIME_SUFFIXES:
            offenders.append(rel)
            continue
        if rel.startswith("data/") and path.name != ".gitkeep":
            offenders.append(rel)
            continue
        if rel.startswith("workspace/") and path.name != ".gitkeep":
            offenders.append(rel)
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            fail(f"cannot read {rel}: {exc}")
        if MERGE_RE.search(raw):
            conflicts.append(rel)
    if conflicts:
        fail("unresolved Git merge markers in: " + ", ".join(sorted(conflicts)))
    if offenders:
        fail("runtime/private artifacts in repository: " + ", ".join(sorted(offenders)))


def main() -> None:
    validate_pyproject()
    validate_tree()
    print(f"REPO_PREFLIGHT=PASS version={EXPECTED_VERSION}")


if __name__ == "__main__":
    main()
