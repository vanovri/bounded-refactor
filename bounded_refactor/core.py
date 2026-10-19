"""Bounded, local refactor runner with a RepairGate acceptance gate."""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import math
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

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
    if not isinstance(config, dict):
        raise RefactorError('config must be an object')
    allowlist = config.get("allowlist")
    if not isinstance(allowlist, list) or not allowlist or not all(isinstance(item, str) and item for item in allowlist):
        raise RefactorError("config.allowlist must be a non-empty list of globs")
    command = config.get("check_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise RefactorError("config.check_command must be a non-empty argv list")
    api_paths = config.get("api_paths", [])
    if not isinstance(api_paths, list) or not all(isinstance(item, str) for item in api_paths):
        raise RefactorError("config.api_paths must be a list of relative paths")
    if any(not relative_is_safe(name) for name in api_paths):
        raise RefactorError('api_paths must stay within the project')
    attempts = config.get("max_attempts", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= MAX_HARD_ATTEMPTS:
        raise RefactorError(f"config.max_attempts must be 1..{MAX_HARD_ATTEMPTS}")
    timeout = config.get("timeout_seconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
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
    if not isinstance(payload, dict):
        raise RefactorError('candidate must be an object')
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
    normalized = name.replace('\\', '/')
    posix, windows = PurePosixPath(normalized), PureWindowsPath(name)
    return (bool(normalized.strip()) and normalized != '.' and not posix.is_absolute()
            and not windows.drive and not windows.root and '..' not in posix.parts
            and ':' not in normalized and '\x00' not in normalized)


def allowed(name: str, allowlist: list[str]) -> bool:
    normalized = name.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in allowlist)


def snapshot(project: Path, target: Path) -> list[dict]:
    if target.exists():
        raise RefactorError('snapshot destination already exists')
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
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.append(node.name + ':' + ast.dump(node.args, include_attributes=False) + ':' + ast.dump(node.returns, include_attributes=False) if node.returns else node.name + ':' + ast.dump(node.args, include_attributes=False))
                else:
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith('_'):
                            names.append(node.name + '.' + member.name + ':' + ast.dump(member.args, include_attributes=False))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        names.append(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                names.append(node.target.id)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    exported = [item.value for item in node.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                    names = [name for name in names if name.split(':')[0].split('.')[0] in exported] + exported
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
    diff = difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}",
    )
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in diff)


def file_manifest(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob('*')
            if p.is_file() and not any(part in {'__pycache__', '.pytest_cache', '.git'} for part in p.relative_to(root).parts)}


def protected(name):
    parts = PurePosixPath(name.replace('\\', '/')).parts
    return any(part in {'.git', '.github', 'tests', 'test', '__pycache__'} for part in parts) or (
        parts[-1].startswith('test_') or parts[-1] in {'conftest.py', 'pytest.ini', 'tox.ini', 'pyproject.toml'})


def execute_check(command, work, timeout):
    """Bound the process tree lifetime; this is not a security sandbox."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, cwd=work, stdout=out, stderr=err, shell=False,
                                   start_new_session=os.name != 'nt',
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Clean up descendants even if the parent exits first on POSIX.
            if os.name == 'nt':
                if process.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait()
        def tail(handle):
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 4000))
            return handle.read().decode('utf-8', errors='replace')
        return {'command': command, 'returncode': None if timed_out else process.returncode,
                'stdout': tail(out), 'stderr': 'timeout' if timed_out else tail(err)}, timed_out


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
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise RefactorError('RepairGate cases must be an array of objects')
    accepted = [case for case in cases if case.get("status") == "accepted"]
    if not accepted:
        raise RefactorError("RepairGate report contains no accepted case")
    if any(case.get('original_unchanged') is not True or not re.fullmatch(r'[0-9a-f]{64}', case.get('source_digest', '')) for case in accepted):
        raise RefactorError('RepairGate accepted cases need source digests and original-preservation evidence')
    return {
        "used": True,
        "status": report.get("status"),
        "accepted_cases": len(accepted),
        "source_digests": [case.get("source_digest") for case in accepted if case.get("source_digest")],
        "provider": report.get("provider"),
        "report_sha256": sha256(path),
        "scope": 'external regression reference only, not bound to this candidate and not an attestation',
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
    output = output.resolve()
    if output == project or output.is_relative_to(project) or project.is_relative_to(output):
        raise RefactorError('output and source directories must not overlap')
    if output.exists() and any(output.iterdir()):
        raise RefactorError('output must be a new or empty directory; existing evidence is never overwritten')
    if not project.is_dir():
        raise RefactorError(f"project is not a directory: {project}")
    for path in project.rglob("*"):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise RefactorError(f"project contains a symlink, which is outside the trusted scope: {path}")
    config = load_config(config_path)
    attempts = load_candidate(candidate_path)
    gate = repairgate_gate(repairgate_report)
    output.mkdir(parents=True, exist_ok=True)
    originals = output / "originals"
    manifest = snapshot(project, originals)
    original_hashes = file_manifest(originals)
    if file_manifest(project) != original_hashes:
        raise RefactorError('source changed while copying')

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
        disallowed = [name for name in attempt["files"] if protected(name) or not allowed(name, config["allowlist"])]
        if disallowed:
            result.status = "rejected"
            result.reason = f"paths outside the allowlist: {', '.join(disallowed)}"
            results.append(result)
            continue
        with tempfile.TemporaryDirectory(prefix="bounded-refactor-") as temp:
            work = Path(temp) / "project"
            shutil.copytree(originals, work, symlinks=False)
            if any(not (work / name).is_file() for name in config['api_paths']):
                raise RefactorError('every api_path must name an existing Python file')
            before = public_api([work / relative for relative in config["api_paths"]])
            for name, content in attempt["files"].items():
                target = work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
            try:
                after = public_api([work / relative for relative in config["api_paths"]])
            except RefactorError as exc:
                result.status, result.reason = 'rejected', str(exc)
                results.append(result)
                continue
            result.api_problems = check_api(before, after)
            if result.api_problems:
                result.status = "rejected"
                result.reason = "; ".join(result.api_problems)
                results.append(result)
                continue
            started_check = time.monotonic()
            frozen = file_manifest(work)
            try:
                check, timed_out = execute_check(config['check_command'], work, config['timeout_seconds'])
                check['duration_ms'] = round((time.monotonic() - started_check) * 1000, 1)
                result.checks.append(check)
                passed = check['returncode'] == 0 and not timed_out
                result.reason = f"check exceeded {config['timeout_seconds']} s" if timed_out else ('' if passed else f"check failed with exit code {check['returncode']}")
            except OSError as exc:
                passed = False
                result.reason = f'check could not start: {exc}'
            if file_manifest(work) != frozen:
                passed = False
                result.reason = 'check changed frozen source or tests'
            result.status = "accepted" if passed else "rejected"
            if passed:
                for name, content in attempt["files"].items():
                    original = (originals / name).read_text(encoding="utf-8") if (originals / name).exists() else ""
                    if original != content:
                        result.diff += unified_diff(original, content, name)
                        result.changed.append(name)
                accepted = result
            results.append(result)
            if passed:
                break

    if file_manifest(project) != original_hashes:
        accepted = None
        results.append(AttemptResult(number=0, files={}, status='rejected', reason='source changed during verification'))
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
        "provenance": "trusted local project only; not a sandbox; check and candidate can use OS permissions/network; source preserved in originals/",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report
