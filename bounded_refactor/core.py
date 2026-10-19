"""Bounded, local refactor runner with a RepairGate acceptance gate."""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_HARD_ATTEMPTS = 5
MAX_TIMEOUT_SECONDS = 120


class RefactorError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RefactorError(f"config is not valid JSON: {exc}") from exc
    allowlist = config.get("allowlist")
    if not isinstance(allowlist, list) or not allowlist or not all(isinstance(item, str) and item for item in allowlist):
        raise RefactorError("config.allowlist must be a non-empty list of globs")
    command = config.get("check_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise RefactorError("config.check_command must be a non-empty argv list")
    api_paths = config.get("api_paths", [])
    if not isinstance(api_paths, list) or not all(isinstance(item, str) for item in api_paths):
        raise RefactorError("config.api_paths must be a list of relative paths")
    attempts = config.get("max_attempts", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= MAX_HARD_ATTEMPTS:
        raise RefactorError(f"config.max_attempts must be 1..{MAX_HARD_ATTEMPTS}")
    timeout = config.get("timeout_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise RefactorError(f"config.timeout_seconds must be >0 and <={MAX_TIMEOUT_SECONDS}")
    return {
        "allowlist": allowlist,
        "check_command": command,
        "api_paths": api_paths,
        "max_attempts": attempts,
        "timeout_seconds": float(timeout),
    }


def load_candidate(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RefactorError(f"candidate is not valid JSON: {exc}") from exc
    attempts = payload.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise RefactorError("candidate.attempts must be a non-empty list")
    normalized = []
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict) or not isinstance(attempt.get("files"), dict):
            raise RefactorError(f"attempt {index} needs a files object")
        files = {}
        for name, content in attempt["files"].items():
            if not isinstance(name, str) or not isinstance(content, str):
                raise RefactorError(f"attempt {index}: file names and contents must be strings")
            files[name] = content
        normalized.append({"files": files, "note": attempt.get("note", "")})
    return normalized


def relative_is_safe(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and name.strip() != ""


def allowed(name: str, allowlist: list[str]) -> bool:
    normalized = name.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in allowlist)


def snapshot(project: Path, target: Path) -> list[dict]:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(project, target, symlinks=False)
    manifest = []
    for path in sorted(target.rglob("*")):
        if path.is_file():
            manifest.append({"path": str(path.relative_to(target)).replace("\\", "/"), "sha256": sha256(path), "bytes": path.stat().st_size})
    (target.parent / "originals-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def public_api(paths: list[Path]) -> dict[str, list[str]]:
    symbols: dict[str, list[str]] = {}
    for path in paths:
        if not path.exists() or path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise RefactorError(f"cannot parse {path}: {exc}") from exc
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        names.append(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                names.append(node.target.id)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    names = [item.value for item in node.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
        symbols[str(path)] = sorted(set(names))
    return symbols


def check_api(before: dict[str, list[str]], after: dict[str, list[str]]) -> list[str]:
    problems = []
    for path, names in before.items():
        missing = sorted(set(names) - set(after.get(path, [])))
        if missing:
            problems.append(f"{path}: removed public symbols: {', '.join(missing)}")
    return problems


def unified_diff(original: str, updated: str, name: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}",
    ))


def repairgate_gate(path: Path | None) -> dict:
    if path is None:
        return {"used": False}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefactorError(f"cannot read RepairGate report: {exc}") from exc
    if report.get("status") != "passed":
        raise RefactorError(f"RepairGate report status is {report.get('status')!r}, not 'passed'")
    cases = report.get("cases") or []
    accepted = [case for case in cases if case.get("status") == "accepted"]
    if not accepted:
        raise RefactorError("RepairGate report contains no accepted case")
    return {
        "used": True,
        "status": report.get("status"),
        "accepted_cases": len(accepted),
        "source_digests": [case.get("source_digest") for case in accepted if case.get("source_digest")],
        "provider": report.get("provider"),
    }


@dataclass
class AttemptResult:
    number: int
    files: dict[str, str]
    checks: list[dict] = field(default_factory=list)
    api_problems: list[str] = field(default_factory=list)
    status: str = "pending"
    reason: str = ""
    diff: str = ""
    changed: list[str] = field(default_factory=list)


def run(project: Path, config_path: Path, candidate_path: Path, output: Path, repairgate_report: Path | None = None) -> dict:
    project = project.resolve()
    if not project.is_dir():
        raise RefactorError(f"project is not a directory: {project}")
    for path in project.rglob("*"):
        if path.is_symlink():
            raise RefactorError(f"project contains a symlink, which is outside the trusted scope: {path}")
    config = load_config(config_path)
    attempts = load_candidate(candidate_path)
    gate = repairgate_gate(repairgate_report)
    output.mkdir(parents=True, exist_ok=True)
    originals = output / "originals"
    manifest = snapshot(project, originals)

    started = time.monotonic()
    results: list[AttemptResult] = []
    accepted: AttemptResult | None = None
    for index, attempt in enumerate(attempts[: config["max_attempts"]], 1):
        result = AttemptResult(number=index, files=attempt["files"])
        unsafe = [name for name in attempt["files"] if not relative_is_safe(name)]
        if unsafe:
            result.status = "rejected"
            result.reason = f"unsafe paths: {', '.join(unsafe)}"
            results.append(result)
            continue
        disallowed = [name for name in attempt["files"] if not allowed(name, config["allowlist"])]
        if disallowed:
            result.status = "rejected"
            result.reason = f"paths outside the allowlist: {', '.join(disallowed)}"
            results.append(result)
            continue
        with tempfile.TemporaryDirectory(prefix="bounded-refactor-") as temp:
            work = Path(temp) / "project"
            shutil.copytree(project, work, symlinks=False)
            before = public_api([work / relative for relative in config["api_paths"]])
            for name, content in attempt["files"].items():
                target = work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
            after = public_api([work / relative for relative in config["api_paths"]])
            result.api_problems = check_api(before, after)
            if result.api_problems:
                result.status = "rejected"
                result.reason = "; ".join(result.api_problems)
                results.append(result)
                continue
            started_check = time.monotonic()
            try:
                completed = subprocess.run(
                    config["check_command"], cwd=work, capture_output=True, text=True,
                    timeout=config["timeout_seconds"], shell=False,
                )
                result.checks.append({
                    "command": config["check_command"], "returncode": completed.returncode,
                    "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:],
                    "duration_ms": round((time.monotonic() - started_check) * 1000, 1),
                })
                passed = completed.returncode == 0
                result.reason = "" if passed else f"check failed with exit code {completed.returncode}"
            except subprocess.TimeoutExpired:
                result.checks.append({"command": config["check_command"], "returncode": None, "stdout": "", "stderr": "timeout", "duration_ms": round((time.monotonic() - started_check) * 1000, 1)})
                passed = False
                result.reason = f"check exceeded {config['timeout_seconds']} s"
            result.status = "accepted" if passed else "rejected"
            if passed:
                for name, content in attempt["files"].items():
                    original = (project / name).read_text(encoding="utf-8") if (project / name).exists() else ""
                    if original != content:
                        result.diff += unified_diff(original, content, name)
                        result.changed.append(name)
                accepted = result
            results.append(result)
            if passed:
                break

    diff_path = output / "accepted.diff"
    diff_path.write_text(accepted.diff if accepted else "", encoding="utf-8", newline="\n")
    report = {
        "schema": "BOUNDED_REFACTOR_V1",
        "project": str(project),
        "accepted": accepted is not None,
        "attempts": [
            {
                "number": item.number, "status": item.status, "reason": item.reason,
                "changed": item.changed, "api_problems": item.api_problems, "checks": item.checks,
            }
            for item in results
        ],
        "changed_files": accepted.changed if accepted else [],
        "diff": str(diff_path.relative_to(output).as_posix()),
        "originals": str(originals.relative_to(output).as_posix()),
        "originals_manifest": manifest,
        "limits": {"max_attempts": config["max_attempts"], "timeout_seconds": config["timeout_seconds"], "allowlist": config["allowlist"]},
        "repairgate": gate,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "provenance": "trusted local project only; no network access; original tree preserved in originals/",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
