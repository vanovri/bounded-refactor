"""Bounded, local refactor runner with a RepairGate acceptance gate."""
from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, Any, NotRequired, TypedDict

logger = logging.getLogger(__name__)

MAX_HARD_ATTEMPTS: int = 5
MAX_TIMEOUT_SECONDS: int = 120
MAX_OUTPUT_TAIL_BYTES: int = 4000

EXCLUDED_MANIFEST_DIRS: frozenset[str] = frozenset({"__pycache__", ".pytest_cache", ".git"})
PROTECTED_DIRS: frozenset[str] = frozenset({".git", ".github", "tests", "test", "__pycache__", ".pytest_cache"})
PROTECTED_FILES: frozenset[str] = frozenset({"conftest.py", "pytest.ini", "tox.ini", "pyproject.toml"})
SHA256_HEX_RE: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")


class RefactorError(ValueError):
    """Raised when configuration, candidate input, or workspace invariants fail."""


class ConfigData(TypedDict):
    allowlist: list[str]
    check_command: list[str]
    api_paths: list[str]
    max_attempts: int
    timeout_seconds: float


class CandidateAttempt(TypedDict):
    files: dict[str, str]
    note: str


class ManifestEntry(TypedDict):
    path: str
    sha256: str
    bytes: int


class CheckResult(TypedDict):
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: NotRequired[float]


class RepairGateSummary(TypedDict):
    used: bool
    status: NotRequired[str]
    accepted_cases: NotRequired[int]
    source_digests: NotRequired[list[str]]
    provider: NotRequired[Any]
    report_sha256: NotRequired[str]
    scope: NotRequired[str]


class AttemptReportEntry(TypedDict):
    number: int
    status: str
    reason: str
    changed: list[str]
    api_problems: list[str]
    checks: list[CheckResult]


class LimitsSummary(TypedDict):
    max_attempts: int
    timeout_seconds: float
    allowlist: list[str]


class RunReport(TypedDict):
    schema: str
    project: str
    accepted: bool
    attempts: list[AttemptReportEntry]
    changed_files: list[str]
    diff: str
    originals: str
    originals_manifest: list[ManifestEntry]
    limits: LimitsSummary
    repairgate: RepairGateSummary
    duration_ms: float
    provenance: str


@dataclass
class AttemptResult:
    number: int
    files: dict[str, str]
    checks: list[CheckResult] = field(default_factory=list)
    api_problems: list[str] = field(default_factory=list)
    status: str = "pending"
    reason: str = ""
    diff: str = ""
    changed: list[str] = field(default_factory=list)

    def to_report_entry(self) -> AttemptReportEntry:
        return {
            "number": self.number,
            "status": self.status,
            "reason": self.reason,
            "changed": list(self.changed),
            "api_problems": list(self.api_problems),
            "checks": list(self.checks),
        }


def sha256(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise RefactorError(f"cannot hash file {path}: {exc}") from exc


def _read_json_file(path: Path, label: str, *, read_error_prefix: str | None = None) -> Any:
    prefix = read_error_prefix or f"cannot read {label}"
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RefactorError(f"{prefix}: {exc}") from exc
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        if read_error_prefix is not None:
            raise RefactorError(f"{read_error_prefix}: {exc}") from exc
        raise RefactorError(f"{label} is not valid JSON: {exc}") from exc


def validate_max_attempts(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_HARD_ATTEMPTS:
        raise RefactorError(f"config.max_attempts must be 1..{MAX_HARD_ATTEMPTS}")
    return value


def validate_timeout_seconds(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= MAX_TIMEOUT_SECONDS
    ):
        raise RefactorError(f"config.timeout_seconds must be >0 and <={MAX_TIMEOUT_SECONDS}")
    return float(value)


def validate_check_command(command: object) -> list[str]:
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
        or not command[0].strip()
        or any("\x00" in item for item in command)
    ):
        raise RefactorError("config.check_command must be a non-empty argv list")
    return list(command)


def load_config(path: Path) -> ConfigData:
    config = _read_json_file(path, "config")
    if not isinstance(config, dict):
        raise RefactorError("config must be an object")
    allowlist = config.get("allowlist")
    if (
        not isinstance(allowlist, list)
        or not allowlist
        or not all(isinstance(item, str) and bool(item.strip()) for item in allowlist)
    ):
        raise RefactorError("config.allowlist must be a non-empty list of globs")
    command = validate_check_command(config.get("check_command"))
    api_paths = config.get("api_paths", [])
    if not isinstance(api_paths, list) or not all(isinstance(item, str) for item in api_paths):
        raise RefactorError("config.api_paths must be a list of relative paths")
    if any(not relative_is_safe(name) for name in api_paths):
        raise RefactorError("api_paths must stay within the project")
    attempts = validate_max_attempts(config.get("max_attempts", 1))
    timeout = validate_timeout_seconds(config.get("timeout_seconds", 20))
    return {
        "allowlist": list(allowlist),
        "check_command": command,
        "api_paths": list(api_paths),
        "max_attempts": attempts,
        "timeout_seconds": timeout,
    }


def _canonical_alias_key(normalized_name: str) -> str:
    if not normalized_name:
        return ""
    return PurePosixPath(normalized_name).as_posix().casefold()


def _normalize_attempt(index: int, attempt: object) -> CandidateAttempt:
    if not isinstance(attempt, dict) or not isinstance(attempt.get("files"), dict):
        raise RefactorError(f"attempt {index} needs a files object")
    note = attempt.get("note", "")
    if not isinstance(note, str):
        raise RefactorError(f"attempt {index}: note must be a string")
    files: dict[str, str] = {}
    seen: set[str] = set()
    for name, content in attempt["files"].items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise RefactorError(f"attempt {index}: file names and contents must be strings")
        normalized_name = name.replace("\\", "/")
        alias_key = _canonical_alias_key(normalized_name)
        if alias_key in seen:
            raise RefactorError("duplicate candidate path aliases")
        files[normalized_name] = content
        seen.add(alias_key)
    return {"files": files, "note": note}


def load_candidate(path: Path) -> list[CandidateAttempt]:
    payload = _read_json_file(path, "candidate")
    if not isinstance(payload, dict):
        raise RefactorError("candidate must be an object")
    attempts = payload.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise RefactorError("candidate.attempts must be a non-empty list")
    return [_normalize_attempt(index, attempt) for index, attempt in enumerate(attempts, 1)]


def relative_is_safe(name: str) -> bool:
    if not isinstance(name, str):
        return False
    normalized = name.replace("\\", "/")
    if (
        not normalized.strip()
        or normalized != normalized.strip()
        or normalized.endswith("/")
        or "//" in normalized
        or ":" in normalized
        or "\x00" in normalized
    ):
        return False
    segments = normalized.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return False
    posix, windows = PurePosixPath(normalized), PureWindowsPath(name)
    return not (posix.is_absolute() or windows.drive or windows.root)


def allowed(name: str, allowlist: Sequence[str]) -> bool:
    normalized = name.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in allowlist)


def snapshot(project: Path, target: Path) -> list[ManifestEntry]:
    if target.exists():
        raise RefactorError("snapshot destination already exists")
    shutil.copytree(project, target, symlinks=False)
    manifest: list[ManifestEntry] = []
    for path in sorted(target.rglob("*")):
        if path.is_file():
            manifest.append(
                {
                    "path": path.relative_to(target).as_posix(),
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
    (target.parent / "originals-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _format_callable_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args_dump = ast.dump(node.args, include_attributes=False)
    if node.returns is not None:
        returns_dump = ast.dump(node.returns, include_attributes=False)
        return f"{node.name}:{args_dump}:{returns_dump}"
    return f"{node.name}:{args_dump}"


def _extract_module_symbols(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            names.append(node.name)
            names.append(_format_callable_signature(node))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            names.append(node.name)
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                    args_dump = ast.dump(member.args, include_attributes=False)
                    names.append(f"{node.name}.{member.name}:{args_dump}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
            names.append(node.target.id)
    return names


def _is_dunder_all_node(node: ast.Assign | ast.AnnAssign) -> bool:
    if isinstance(node, ast.Assign):
        return any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    return isinstance(node.target, ast.Name) and node.target.id == "__all__"


def _extract_dunder_all(tree: ast.Module) -> list[str] | None:
    exported: list[str] | None = None
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and _is_dunder_all_node(node)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            exported = [
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
    return exported


def public_api(paths: Sequence[Path]) -> dict[str, list[str]]:
    symbols: dict[str, list[str]] = {}
    for path in paths:
        if not path.exists() or path.suffix != ".py":
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, ValueError, OSError) as exc:
            raise RefactorError(f"cannot parse {path}: {exc}") from exc
        names = _extract_module_symbols(tree)
        exported = _extract_dunder_all(tree)
        if exported is not None:
            exported_set = frozenset(exported)
            names = [name for name in names if name.split(":")[0].split(".")[0] in exported_set] + exported
        symbols[str(path)] = sorted(set(names))
    return symbols


def check_api(before: Mapping[str, Sequence[str]], after: Mapping[str, Sequence[str]]) -> list[str]:
    problems: list[str] = []
    for path, names in before.items():
        missing = sorted(set(names) - set(after.get(path, ())))
        if missing:
            problems.append(f"{path}: removed public symbols: {', '.join(missing)}")
    return problems


def unified_diff(original: str, updated: str, name: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
    )
    return "".join(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in diff)


def file_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for current_dir, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_MANIFEST_DIRS)
        base = Path(current_dir)
        for filename in sorted(filenames):
            file_path = base / filename
            if file_path.is_file():
                manifest[file_path.relative_to(root).as_posix()] = sha256(file_path)
    return manifest


def protected(name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts:
        return False
    lowered = tuple(part.casefold() for part in parts)
    return any(part in PROTECTED_DIRS for part in lowered) or (
        lowered[-1].startswith("test_") or lowered[-1] in PROTECTED_FILES
    )


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        if process.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            except OSError as exc:
                logger.warning("taskkill failed for pid %s: %s", process.pid, exc)
    else:
        kill_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.killpg(process.pid, kill_sig)  # type: ignore[attr-defined]
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    process.wait()


def _read_stream_tail(handle: IO[bytes], max_bytes: int = MAX_OUTPUT_TAIL_BYTES) -> str:
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - max_bytes))
    return handle.read().decode("utf-8", errors="replace")


def execute_check(command: Sequence[str], work: Path, timeout: float) -> tuple[CheckResult, bool]:
    """Bound the process tree lifetime; this is not a security sandbox."""
    validated_command = validate_check_command(list(command) if isinstance(command, Sequence) and not isinstance(command, (str, bytes)) else command)
    validated_timeout = validate_timeout_seconds(timeout)
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        process = subprocess.Popen(
            validated_command,
            cwd=work,
            stdout=out_file,
            stderr=err_file,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
        timed_out = False
        try:
            process.wait(timeout=validated_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning("Check command timed out after %s s: %s", validated_timeout, validated_command)
        finally:
            _terminate_process_tree(process)
        result: CheckResult = {
            "command": validated_command,
            "returncode": None if timed_out else process.returncode,
            "stdout": _read_stream_tail(out_file),
            "stderr": "timeout" if timed_out else _read_stream_tail(err_file),
        }
        return result, timed_out


def _is_valid_accepted_case(case: Mapping[str, Any]) -> bool:
    digest = case.get("source_digest")
    return case.get("original_unchanged") is True and isinstance(digest, str) and bool(SHA256_HEX_RE.fullmatch(digest))


def repairgate_gate(path: Path | None) -> RepairGateSummary:
    if path is None:
        return {"used": False}
    report = _read_json_file(path, "RepairGate report", read_error_prefix="cannot read RepairGate report")
    if not isinstance(report, dict):
        raise RefactorError("RepairGate report must be an object")
    status = report.get("status")
    if status != "passed":
        raise RefactorError(f"RepairGate report status is {status!r}, not 'passed'")
    cases = report.get("cases", [])
    if cases is None:
        cases = []
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise RefactorError("RepairGate cases must be an array of objects")
    accepted = [case for case in cases if case.get("status") == "accepted"]
    if not accepted:
        raise RefactorError("RepairGate report contains no accepted case")
    if not all(_is_valid_accepted_case(case) for case in accepted):
        raise RefactorError("RepairGate accepted cases need source digests and original-preservation evidence")
    return {
        "used": True,
        "status": status,
        "accepted_cases": len(accepted),
        "source_digests": [str(case["source_digest"]) for case in accepted if case.get("source_digest")],
        "provider": report.get("provider"),
        "report_sha256": sha256(path),
        "scope": "external regression reference only, not bound to this candidate and not an attestation",
    }


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _validate_project_and_output(project: Path, output: Path) -> tuple[Path, Path]:
    raw_project = project
    resolved_project = project.resolve()
    resolved_output = output.resolve()
    if (
        resolved_output == resolved_project
        or resolved_output.is_relative_to(resolved_project)
        or resolved_project.is_relative_to(resolved_output)
    ):
        raise RefactorError("output and source directories must not overlap")
    if resolved_output.exists() and any(resolved_output.iterdir()):
        raise RefactorError("output must be a new or empty directory; existing evidence is never overwritten")
    if not resolved_project.is_dir():
        raise RefactorError(f"project is not a directory: {resolved_project}")
    if _is_link_or_junction(raw_project):
        raise RefactorError(f"project contains a symlink, which is outside the trusted scope: {raw_project}")
    for entry in resolved_project.rglob("*"):
        if _is_link_or_junction(entry):
            raise RefactorError(f"project contains a symlink, which is outside the trusted scope: {entry}")
    return resolved_project, resolved_output


def _validate_attempt_paths(files: Mapping[str, str], allowlist: Sequence[str]) -> str | None:
    unsafe = [name for name in files if not relative_is_safe(name)]
    if unsafe:
        return f"unsafe paths: {', '.join(unsafe)}"
    disallowed = [name for name in files if protected(name) or not allowed(name, allowlist)]
    if disallowed:
        return f"paths outside the allowlist: {', '.join(disallowed)}"
    return None


def _apply_candidate_files(work: Path, files: Mapping[str, str]) -> None:
    for name, content in files.items():
        target = work / name
        if target.exists() and target.is_dir():
            raise RefactorError(f"candidate path collides with an existing directory: {name}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise RefactorError(f"cannot write candidate file {name}: {exc}") from exc


def _collect_attempt_diff(originals: Path, files: Mapping[str, str]) -> tuple[str, list[str]]:
    combined_diff: list[str] = []
    changed_files: list[str] = []
    for name, content in files.items():
        original_path = originals / name
        original = original_path.read_text(encoding="utf-8", errors="replace") if original_path.exists() else ""
        if original != content:
            combined_diff.append(unified_diff(original, content, name))
            changed_files.append(name)
    return "".join(combined_diff), changed_files


def _run_workspace_check(result: AttemptResult, work: Path, config: ConfigData) -> bool:
    started_check = time.monotonic()
    frozen = file_manifest(work)
    try:
        check, timed_out = execute_check(config["check_command"], work, config["timeout_seconds"])
        check["duration_ms"] = round((time.monotonic() - started_check) * 1000, 1)
        result.checks.append(check)
        passed = check["returncode"] == 0 and not timed_out
        if timed_out:
            result.reason = f"check exceeded {config['timeout_seconds']} s"
        elif not passed:
            result.reason = f"check failed with exit code {check['returncode']}"
    except OSError as exc:
        logger.warning("Check command failed to start: %s", exc)
        passed = False
        result.reason = f"check could not start: {exc}"
    if file_manifest(work) != frozen:
        logger.warning("Check mutated frozen source or tests in %s", work)
        passed = False
        result.reason = "check changed frozen source or tests"
    return passed


def _evaluate_attempt_in_workspace(result: AttemptResult, originals: Path, config: ConfigData) -> bool:
    with tempfile.TemporaryDirectory(prefix="bounded-refactor-") as temp_dir:
        work = Path(temp_dir) / "project"
        shutil.copytree(originals, work, symlinks=False)
        if any(not (work / name).is_file() for name in config["api_paths"]):
            raise RefactorError("every api_path must name an existing Python file")
        before = public_api([work / relative for relative in config["api_paths"]])
        try:
            _apply_candidate_files(work, result.files)
            after = public_api([work / relative for relative in config["api_paths"]])
        except RefactorError as exc:
            result.status = "rejected"
            result.reason = str(exc)
            return False
        result.api_problems = check_api(before, after)
        if result.api_problems:
            result.status = "rejected"
            result.reason = "; ".join(result.api_problems)
            return False
        passed = _run_workspace_check(result, work, config)
        result.status = "accepted" if passed else "rejected"
        if passed:
            result.diff, result.changed = _collect_attempt_diff(originals, result.files)
        return passed


def _evaluate_attempt(number: int, attempt: CandidateAttempt, originals: Path, config: ConfigData) -> AttemptResult:
    result = AttemptResult(number=number, files=attempt["files"])
    path_problem = _validate_attempt_paths(attempt["files"], config["allowlist"])
    if path_problem is not None:
        result.status = "rejected"
        result.reason = path_problem
        return result
    _evaluate_attempt_in_workspace(result, originals, config)
    return result


def _build_run_report(
    *,
    project: Path,
    output: Path,
    originals: Path,
    diff_path: Path,
    manifest: list[ManifestEntry],
    config: ConfigData,
    gate: RepairGateSummary,
    results: Sequence[AttemptResult],
    accepted: AttemptResult | None,
    started: float,
) -> RunReport:
    return {
        "schema": "BOUNDED_REFACTOR_V1",
        "project": str(project),
        "accepted": accepted is not None,
        "attempts": [item.to_report_entry() for item in results],
        "changed_files": list(accepted.changed) if accepted is not None else [],
        "diff": diff_path.relative_to(output).as_posix(),
        "originals": originals.relative_to(output).as_posix(),
        "originals_manifest": manifest,
        "limits": {
            "max_attempts": config["max_attempts"],
            "timeout_seconds": config["timeout_seconds"],
            "allowlist": list(config["allowlist"]),
        },
        "repairgate": gate,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "provenance": (
            "trusted local project only; not a sandbox; check and candidate can use OS "
            "permissions/network; source preserved in originals/"
        ),
    }


def run(
    project: Path,
    config_path: Path,
    candidate_path: Path,
    output: Path,
    repairgate_report: Path | None = None,
) -> RunReport:
    resolved_project, resolved_output = _validate_project_and_output(project, output)
    config = load_config(config_path)
    attempts = load_candidate(candidate_path)
    gate = repairgate_gate(repairgate_report)
    resolved_output.mkdir(parents=True, exist_ok=True)
    originals = resolved_output / "originals"
    manifest = snapshot(resolved_project, originals)
    original_hashes = file_manifest(originals)
    if file_manifest(resolved_project) != original_hashes:
        raise RefactorError("source changed while copying")

    started = time.monotonic()
    results: list[AttemptResult] = []
    accepted: AttemptResult | None = None
    for index, attempt in enumerate(attempts[: config["max_attempts"]], 1):
        result = _evaluate_attempt(index, attempt, originals, config)
        results.append(result)
        if result.status == "accepted":
            accepted = result
            break

    if file_manifest(resolved_project) != original_hashes:
        logger.warning("Original source directory changed during verification: %s", resolved_project)
        accepted = None
        results.append(AttemptResult(number=0, files={}, status="rejected", reason="source changed during verification"))

    diff_path = resolved_output / "accepted.diff"
    diff_path.write_text(accepted.diff if accepted is not None else "", encoding="utf-8", newline="\n")
    report = _build_run_report(
        project=resolved_project,
        output=resolved_output,
        originals=originals,
        diff_path=diff_path,
        manifest=manifest,
        config=config,
        gate=gate,
        results=results,
        accepted=accepted,
        started=started,
    )
    (resolved_output / "report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return report
