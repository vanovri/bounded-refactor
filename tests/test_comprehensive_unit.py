"""Comprehensive offline unit and characterization tests for bounded_refactor."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bounded_refactor.cli import build_parser, format_cli_summary, main
from bounded_refactor.core import (
    MAX_HARD_ATTEMPTS,
    MAX_TIMEOUT_SECONDS,
    RefactorError,
    allowed,
    check_api,
    execute_check,
    file_manifest,
    load_candidate,
    load_config,
    protected,
    public_api,
    relative_is_safe,
    repairgate_gate,
    run,
    sha256,
    snapshot,
    unified_diff,
    validate_check_command,
    validate_max_attempts,
    validate_timeout_seconds,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def make_project(tmp_path: Path) -> Path:
    target = tmp_path / "project"
    shutil.copytree(FIXTURES / "project", target)
    return target


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_config(tmp_path: Path, name: str = "config.json", **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "allowlist": ["pkg/*.py"],
        "check_command": ["mock-python", "-m", "check"],
        "api_paths": ["pkg/mathutil.py"],
        "max_attempts": 3,
        "timeout_seconds": 10,
    }
    payload.update(overrides)
    return write_json(tmp_path / name, payload)


def make_candidate(tmp_path: Path, attempts: list[dict[str, Any]], name: str = "candidate.json") -> Path:
    return write_json(tmp_path / name, {"attempts": attempts})


# ---------------------------------------------------------------------------
# 1. Hashing, Manifest, & Snapshot Tests
# ---------------------------------------------------------------------------


def test_sha256_empty_and_large_streaming_file(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.bin"
    empty_file.write_bytes(b"")
    assert sha256(empty_file) == hashlib.sha256(b"").hexdigest()

    large_payload = b"bounded-refactor-chunk-" * 120_000  # ~2.76 MiB (> 1 MiB chunk)
    large_file = tmp_path / "large.bin"
    large_file.write_bytes(large_payload)
    assert sha256(large_file) == hashlib.sha256(large_payload).hexdigest()

    with pytest.raises(RefactorError, match="cannot hash file"):
        sha256(tmp_path / "missing_hash_target.bin")


def test_file_manifest_prunes_excluded_dirs_without_descending(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")

    for excluded in ("__pycache__", ".pytest_cache", ".git"):
        nested = root / excluded / "deep" / "sub"
        nested.mkdir(parents=True)
        (nested / "ignored.bin").write_bytes(b"ignore-me")

    visited_dirs: list[str] = []
    real_walk = os.walk

    def tracking_walk(top: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            visited_dirs.append(Path(dirpath).name)
            yield dirpath, dirnames, filenames

    with patch("bounded_refactor.core.os.walk", side_effect=tracking_walk):
        manifest = file_manifest(root)

    assert set(manifest.keys()) == {"pkg/mod.py", ".gitignore", ".github/workflows/ci.yml"}
    assert {"tree", "pkg", ".github", "workflows"}.issubset(set(visited_dirs))
    for excluded in ("__pycache__", ".pytest_cache", ".git", "deep", "sub"):
        assert excluded not in visited_dirs


def test_snapshot_rejects_existing_destination_and_writes_manifest(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    existing = tmp_path / "already_there"
    existing.mkdir()
    with pytest.raises(RefactorError, match="already exists"):
        snapshot(project, existing)

    dest = tmp_path / "snap" / "originals"
    dest.parent.mkdir(parents=True)
    entries = snapshot(project, dest)
    paths = [entry["path"] for entry in entries]
    assert "pkg/mathutil.py" in paths
    saved_manifest = json.loads((dest.parent / "originals-manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest == entries


# ---------------------------------------------------------------------------
# 2. Config & Candidate Validation (Edge Cases, Bool Traps, Malformed Payloads)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_attempts",
    [True, False, 0, -1, MAX_HARD_ATTEMPTS + 1, 1.5, "2", None],
)
def test_load_config_rejects_invalid_max_attempts_including_bool(tmp_path: Path, bad_attempts: Any) -> None:
    cfg = make_config(tmp_path, max_attempts=bad_attempts)
    with pytest.raises(RefactorError, match="max_attempts"):
        load_config(cfg)


@pytest.mark.parametrize(
    "bad_timeout",
    [True, False, 0, -0.01, MAX_TIMEOUT_SECONDS + 0.01, "10", None],
)
def test_load_config_rejects_invalid_timeout_including_bool(tmp_path: Path, bad_timeout: Any) -> None:
    cfg = make_config(tmp_path, timeout_seconds=bad_timeout)
    with pytest.raises(RefactorError, match="timeout_seconds"):
        load_config(cfg)


@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
def test_load_config_rejects_nan_and_infinite_timeout(tmp_path: Path, non_finite: str) -> None:
    path = tmp_path / "nan_config.json"
    path.write_text(
        f'{{"allowlist": ["*.py"], "check_command": ["python"], "timeout_seconds": {non_finite}}}',
        encoding="utf-8",
    )
    with pytest.raises(RefactorError, match="timeout_seconds"):
        load_config(path)


def test_load_config_accepts_exact_upper_bounds_and_defaults(tmp_path: Path) -> None:
    minimal = write_json(
        tmp_path / "min.json",
        {
            "allowlist": ["pkg/*.py"],
            "check_command": ["python"],
            "max_attempts": MAX_HARD_ATTEMPTS,
            "timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
    )
    parsed = load_config(minimal)
    assert parsed["max_attempts"] == MAX_HARD_ATTEMPTS
    assert parsed["timeout_seconds"] == float(MAX_TIMEOUT_SECONDS)
    assert parsed["api_paths"] == []


def test_load_config_malformed_json_non_dict_missing_file_and_bad_utf8(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(RefactorError):
        load_config(missing)

    bad_utf8 = tmp_path / "bad_utf8.json"
    bad_utf8.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(RefactorError):
        load_config(bad_utf8)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RefactorError, match="not valid JSON"):
        load_config(malformed)

    non_dict = write_json(tmp_path / "list.json", ["not", "an", "object"])
    with pytest.raises(RefactorError, match="must be an object"):
        load_config(non_dict)

    bad_api_paths = make_config(tmp_path, name="bad_api.json", api_paths=[123])
    with pytest.raises(RefactorError, match="api_paths"):
        load_config(bad_api_paths)

    empty_exec = make_config(tmp_path, name="empty_exec.json", check_command=[""])
    with pytest.raises(RefactorError, match="check_command"):
        load_config(empty_exec)


def test_load_candidate_malformed_missing_non_dict_and_invalid_entries(tmp_path: Path) -> None:
    with pytest.raises(RefactorError):
        load_candidate(tmp_path / "missing_candidate.json")

    bad_utf8 = tmp_path / "bad_candidate_utf8.json"
    bad_utf8.write_bytes(b"\x80\x81")
    with pytest.raises(RefactorError):
        load_candidate(bad_utf8)

    malformed = tmp_path / "malformed_cand.json"
    malformed.write_text("{broken", encoding="utf-8")
    with pytest.raises(RefactorError, match="not valid JSON"):
        load_candidate(malformed)

    non_dict = write_json(tmp_path / "cand_list.json", [])
    with pytest.raises(RefactorError, match="must be an object"):
        load_candidate(non_dict)

    no_files_obj = write_json(tmp_path / "cand_nofiles.json", {"attempts": [{"files": []}]})
    with pytest.raises(RefactorError, match="needs a files object"):
        load_candidate(no_files_obj)

    bad_note = write_json(
        tmp_path / "cand_bad_note.json",
        {"attempts": [{"files": {"pkg/mathutil.py": "x = 1\n"}, "note": 123}]},
    )
    with pytest.raises(RefactorError, match="note"):
        load_candidate(bad_note)


@pytest.mark.parametrize(
    ("first_key", "second_key"),
    [
        ("pkg/mathutil.py", "PKG/MATHUTIL.PY"),
        ("pkg\\mathutil.py", "pkg/MathUtil.py"),
        ("./pkg/mathutil.py", "pkg/mathutil.py"),
        ("pkg//mathutil.py", "pkg/mathutil.py"),
    ],
)
def test_load_candidate_rejects_duplicate_aliases_and_preserves_order(
    tmp_path: Path, first_key: str, second_key: str
) -> None:
    cand = make_candidate(
        tmp_path,
        [
            {
                "files": {
                    "pkg/alpha.py": "a = 1\n",
                    first_key: "x = 1\n",
                    "pkg/beta.py": "b = 2\n",
                    second_key: "x = 2\n",
                }
            }
        ],
    )
    with pytest.raises(RefactorError, match="duplicate candidate path aliases"):
        load_candidate(cand)

    valid = make_candidate(
        tmp_path,
        [{"note": "ordered", "files": {"pkg\\z.py": "z=1\n", "pkg/a.py": "a=1\n", "pkg/m.py": "m=1\n"}}],
    )
    loaded = load_candidate(valid)
    assert list(loaded[0]["files"].keys()) == ["pkg/z.py", "pkg/a.py", "pkg/m.py"]
    assert loaded[0]["note"] == "ordered"


# ---------------------------------------------------------------------------
# 3. Path Safety, Allowlist, & Protected File Contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "",
        "   ",
        ".",
        "..",
        "../secret.py",
        "..\\secret.py",
        "pkg/../../secret.py",
        "./pkg/mathutil.py",
        "pkg/./mathutil.py",
        "pkg//mathutil.py",
        "pkg/mathutil.py/",
        "/etc/passwd",
        "\\Windows\\System32\\cmd.exe",
        "C:\\abs\\file.py",
        "D:drive_relative.py",
        "pkg/file.py:Zone.Identifier",
        "pkg/null\x00byte.py",
        " pkg/leading_space.py",
        "pkg/trailing_space.py ",
    ],
)
def test_relative_is_safe_rejects_unsafe_variants(unsafe_path: str) -> None:
    assert relative_is_safe(unsafe_path) is False


@pytest.mark.parametrize("safe_path", ["pkg/mathutil.py", "pkg\\sub\\helper.py", "module.py", "src/my file.py"])
def test_relative_is_safe_accepts_valid_relative_paths(safe_path: str) -> None:
    assert relative_is_safe(safe_path) is True


@pytest.mark.parametrize(
    "protected_path",
    [
        "tests/test_core.py",
        "TESTS/test_core.py",
        "test/helper.py",
        "pkg/test_inline.py",
        "pkg/TEST_INLINE.PY",
        "conftest.py",
        "pkg/CONFTEST.PY",
        "pytest.ini",
        "tox.ini",
        "pyproject.toml",
        "PyProject.toml",
        ".git/config",
        ".GIT/HEAD",
        ".github/workflows/verify.yml",
        ".GitHub/workflows/verify.yml",
        "pkg/__pycache__/mod.pyc",
    ],
)
def test_protected_identifies_protected_paths_case_insensitively(protected_path: str) -> None:
    assert protected(protected_path) is True


def test_protected_handles_empty_and_normal_source_files() -> None:
    assert protected("") is False
    assert protected(".") is False
    assert relative_is_safe(123) is False  # type: ignore[arg-type]
    assert protected("pkg/mathutil.py") is False
    assert allowed("pkg\\mathutil.py", ["pkg/*.py"]) is True
    assert allowed("other/mathutil.py", ["pkg/*.py"]) is False


# ---------------------------------------------------------------------------
# 4. AST Public API Inspection & Diff Helpers
# ---------------------------------------------------------------------------


def test_public_api_covers_classes_methods_constants_async_and_dunder_all(tmp_path: Path) -> None:
    mod = tmp_path / "service.py"
    mod.write_text(
        "PUBLIC_CONST = 42\n"
        "TYPED_VAR: str = 'ok'\n"
        "_PRIVATE_VAR = 0\n"
        "def _internal() -> None:\n    pass\n"
        "async def fetch(url: str, retries: int = 2) -> str:\n    return url\n"
        "class Client:\n"
        "    def send(self, payload: bytes) -> int:\n        return len(payload)\n"
        "    def _secret(self) -> None:\n        pass\n",
        encoding="utf-8",
    )
    non_py = tmp_path / "notes.txt"
    non_py.write_text("ignored", encoding="utf-8")
    missing = tmp_path / "missing.py"

    before = public_api([mod, non_py, missing])
    assert str(non_py) not in before
    assert str(missing) not in before
    entries = before[str(mod)]
    assert "PUBLIC_CONST" in entries
    assert "TYPED_VAR" in entries
    assert "_PRIVATE_VAR" not in entries
    assert "Client" in entries
    assert any(item.startswith("Client.send:") for item in entries)
    assert not any("_secret" in item or "_internal" in item for item in entries)

    # Modify method signature -> check_api detects removal
    mod.write_text(
        "PUBLIC_CONST = 42\n"
        "TYPED_VAR: str = 'ok'\n"
        "async def fetch(url: str, retries: int = 2) -> str:\n    return url\n"
        "class Client:\n"
        "    def send(self, payload: bytes, extra: bool) -> int:\n        return len(payload)\n",
        encoding="utf-8",
    )
    problems = check_api(before, public_api([mod]))
    assert len(problems) == 1
    assert "Client.send" in problems[0]


def test_public_api_supports_annotated_dunder_all_and_rejects_bad_encoding(tmp_path: Path) -> None:
    mod = tmp_path / "exported.py"
    mod.write_text(
        "__all__: list[str] = ['run_task']\n"
        "def run_task(x: int) -> int:\n    return x\n"
        "def unexported_helper(y: int) -> int:\n    return y\n",
        encoding="utf-8",
    )
    api = public_api([mod])[str(mod)]
    assert "run_task" in api
    assert not any("unexported_helper" in item for item in api)

    plain_mod = tmp_path / "plain_exported.py"
    plain_mod.write_text(
        "__all__ = ('run_task',)\n"
        "def run_task(x: int) -> int:\n    return x\n"
        "def hidden_helper() -> None:\n    pass\n",
        encoding="utf-8",
    )
    plain_api = public_api([plain_mod])[str(plain_mod)]
    assert "run_task" in plain_api
    assert not any("hidden_helper" in item for item in plain_api)

    bad = tmp_path / "bad.py"
    bad.write_bytes(b"\xff\xfeinvalid")
    with pytest.raises(RefactorError, match="cannot parse"):
        public_api([bad])


def test_unified_diff_formats_trailing_and_missing_newline() -> None:
    diff_with_nl = unified_diff("a = 1\n", "a = 2\n", "pkg/a.py")
    assert "\\ No newline at end of file" not in diff_with_nl
    diff_without_nl = unified_diff("a = 1", "a = 2", "pkg/a.py")
    assert "\\ No newline at end of file" in diff_without_nl


# ---------------------------------------------------------------------------
# 5. RepairGate Gate Validation
# ---------------------------------------------------------------------------


def test_repairgate_gate_edge_cases(tmp_path: Path) -> None:
    assert repairgate_gate(None) == {"used": False}

    with pytest.raises(RefactorError, match="cannot read RepairGate report"):
        repairgate_gate(tmp_path / "missing_gate.json")

    bad_utf8 = tmp_path / "bad_gate.json"
    bad_utf8.write_bytes(b"\xff\xfe")
    with pytest.raises(RefactorError, match="cannot read RepairGate report"):
        repairgate_gate(bad_utf8)

    malformed_gate = tmp_path / "malformed_gate.json"
    malformed_gate.write_text("{broken", encoding="utf-8")
    with pytest.raises(RefactorError, match="cannot read RepairGate report"):
        repairgate_gate(malformed_gate)

    none_cases = write_json(tmp_path / "none_cases.json", {"status": "passed", "cases": None})
    with pytest.raises(RefactorError, match="no accepted case"):
        repairgate_gate(none_cases)

    empty_key_cand = make_candidate(tmp_path, [{"files": {"": "x = 1\n"}}], name="empty_key.json")
    assert load_candidate(empty_key_cand)[0]["files"] == {"": "x = 1\n"}

    non_dict = write_json(tmp_path / "list_gate.json", ["not-a-dict"])
    with pytest.raises(RefactorError, match="object"):
        repairgate_gate(non_dict)

    falsy_cases = write_json(tmp_path / "falsy_cases.json", {"status": "passed", "cases": False})
    with pytest.raises(RefactorError, match="array of objects"):
        repairgate_gate(falsy_cases)

    no_accepted = write_json(
        tmp_path / "no_accepted.json",
        {"status": "passed", "cases": [{"status": "rejected"}]},
    )
    with pytest.raises(RefactorError, match="no accepted case"):
        repairgate_gate(no_accepted)

    null_digest = write_json(
        tmp_path / "null_digest.json",
        {
            "status": "passed",
            "cases": [{"status": "accepted", "original_unchanged": True, "source_digest": None}],
        },
    )
    with pytest.raises(RefactorError, match="source digests"):
        repairgate_gate(null_digest)


# ---------------------------------------------------------------------------
# 6. Mocked Offline Subprocess Execution (`execute_check`)
# ---------------------------------------------------------------------------


def test_execute_check_rejects_bool_and_invalid_bounds(tmp_path: Path) -> None:
    with pytest.raises(RefactorError, match="timeout"):
        execute_check(["python"], tmp_path, True)
    with pytest.raises(RefactorError, match="timeout"):
        execute_check(["python"], tmp_path, -5)
    with pytest.raises(RefactorError, match="check_command"):
        execute_check([], tmp_path, 5)


def test_execute_check_mocks_windows_timeout_cleanup_and_tail_truncation(tmp_path: Path) -> None:
    def fake_popen(command: Any, cwd: Any, stdout: Any, stderr: Any, **kwargs: Any) -> MagicMock:
        stdout.write(b"X" * 5000 + b"TAIL_MARKER")
        stdout.flush()
        stderr.write(b"err-output")
        stderr.flush()
        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = None
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd=command, timeout=1.0), 0]
        proc.poll.side_effect = [None, None]
        return proc

    with (
        patch("bounded_refactor.core.os.name", "nt"),
        patch("bounded_refactor.core.subprocess.Popen", side_effect=fake_popen),
        patch("bounded_refactor.core.subprocess.run") as mock_taskkill,
    ):
        result, timed_out = execute_check(["mock-cmd"], tmp_path, 1.0)

    assert timed_out is True
    assert result["returncode"] is None
    assert result["stderr"] == "timeout"
    assert len(result["stdout"]) == 4000
    assert result["stdout"].endswith("TAIL_MARKER")
    mock_taskkill.assert_called_once()


def test_execute_check_mocks_posix_process_group_cleanup(tmp_path: Path) -> None:
    def fake_popen(command: Any, cwd: Any, stdout: Any, stderr: Any, **kwargs: Any) -> MagicMock:
        stdout.write(b"ok\n")
        stdout.flush()
        proc = MagicMock()
        proc.pid = 9999
        proc.returncode = 0
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        return proc

    with (
        patch("bounded_refactor.core.os.name", "posix"),
        patch("bounded_refactor.core.subprocess.Popen", side_effect=fake_popen),
        patch("bounded_refactor.core.os.killpg", create=True, side_effect=ProcessLookupError) as mock_killpg,
    ):
        result, timed_out = execute_check(["mock-cmd"], tmp_path, 2.0)

    assert timed_out is False
    assert result["returncode"] == 0
    assert result["stdout"] == "ok\n"
    mock_killpg.assert_called_once()


# ---------------------------------------------------------------------------
# 7. End-to-End Offline `run()` Retry Pipeline, Symlinks, & CLI Tests
# ---------------------------------------------------------------------------


def test_run_bounded_retry_loop_with_mocked_check_exports_only_accepted_diff(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    original_bytes = (project / "pkg" / "mathutil.py").read_bytes()

    attempts = [
        {"note": "bad syntax", "files": {"pkg/mathutil.py": "def add(:\n"}},
        {"note": "fails check", "files": {"pkg/mathutil.py": "def add(a, b):\n    return a * b\n"}},
        {
            "note": "fixes add and adds helper module",
            "files": {
                "pkg/mathutil.py": "def add(a, b):\n    return a + b\n",
                "pkg/extra.py": "VALUE = 1\n",
            },
        },
        {"note": "never reached", "files": {"pkg/mathutil.py": "def add(a, b):\n    return 0\n"}},
    ]
    call_count = 0

    def fake_execute_check(command: list[str], work: Path, timeout: float) -> tuple[dict[str, Any], bool]:
        nonlocal call_count
        call_count += 1
        code = 1 if call_count == 1 else 0
        return {"command": command, "returncode": code, "stdout": "", "stderr": ""}, False

    out_dir = tmp_path / "out"
    with patch("bounded_refactor.core.execute_check", side_effect=fake_execute_check):
        report = run(project, make_config(tmp_path, max_attempts=4), make_candidate(tmp_path, attempts), out_dir)

    assert report["accepted"] is True
    assert call_count == 2  # attempt 1 failed AST check before subprocess; attempt 4 skipped after acceptance
    assert [item["status"] for item in report["attempts"]] == ["rejected", "rejected", "accepted"]
    assert report["changed_files"] == ["pkg/mathutil.py", "pkg/extra.py"]
    assert (project / "pkg" / "mathutil.py").read_bytes() == original_bytes
    patch_text = (out_dir / "accepted.diff").read_text(encoding="utf-8")
    assert "+    return a + b" in patch_text
    assert "+VALUE = 1" in patch_text


def test_run_noop_candidate_produces_empty_changed_files_and_diff(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    same_content = (project / "pkg" / "mathutil.py").read_text(encoding="utf-8")
    out_dir = tmp_path / "out"
    with patch(
        "bounded_refactor.core.execute_check",
        return_value=({"command": ["ok"], "returncode": 0, "stdout": "", "stderr": ""}, False),
    ):
        report = run(
            project,
            make_config(tmp_path),
            make_candidate(tmp_path, [{"files": {"pkg/mathutil.py": same_content}}]),
            out_dir,
        )
    assert report["accepted"] is True
    assert report["changed_files"] == []
    assert (out_dir / "accepted.diff").read_text(encoding="utf-8") == ""


def test_run_rejects_non_directory_project_missing_api_path_and_symlinks(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("hi", encoding="utf-8")
    cand = make_candidate(tmp_path, [{"files": {"pkg/mathutil.py": "def add(a, b):\n    return a + b\n"}}])
    with pytest.raises(RefactorError, match="not a directory"):
        run(not_a_dir, make_config(tmp_path), cand, tmp_path / "out1")

    project = make_project(tmp_path)
    with pytest.raises(RefactorError, match="api_path"):
        run(project, make_config(tmp_path, name="missing_api.json", api_paths=["pkg/nope.py"]), cand, tmp_path / "out2")

    with (
        patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p.name == "mathutil.py"),
        pytest.raises(RefactorError, match="symlink"),
    ):
        run(project, make_config(tmp_path), cand, tmp_path / "out3")


def test_run_detects_concurrent_source_modification_during_verification(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    cand = make_candidate(tmp_path, [{"files": {"pkg/mathutil.py": "def add(a, b):\n    return a + b\n"}}])

    def mutating_check(command: list[str], work: Path, timeout: float) -> tuple[dict[str, Any], bool]:
        # Simulate external process mutating the original project mid-run
        (project / "pkg" / "mathutil.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}, False

    with patch("bounded_refactor.core.execute_check", side_effect=mutating_check):
        report = run(project, make_config(tmp_path), cand, tmp_path / "out")

    assert report["accepted"] is False
    assert report["attempts"][-1]["reason"] == "source changed during verification"


def test_cli_returns_exit_code_2_on_refactor_error_and_prints_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path)
    bad_cfg = write_json(tmp_path / "bad_cfg.json", {"allowlist": [], "check_command": ["python"]})
    cand = make_candidate(tmp_path, [{"files": {"pkg/mathutil.py": "def add(a, b):\n    return a + b\n"}}])
    code = main(
        [
            "run",
            "--project",
            str(project),
            "--config",
            str(bad_cfg),
            "--candidate",
            str(cand),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "bounded-refactor:" in captured.err


def test_validator_helpers_and_cli_helpers_direct_contracts(tmp_path: Path) -> None:
    assert validate_max_attempts(3) == 3
    assert validate_timeout_seconds(15) == 15.0
    assert validate_check_command(["python", "-V"]) == ["python", "-V"]
    with pytest.raises(RefactorError):
        validate_check_command(["python", "bad\x00arg"])

    parser = build_parser()
    parsed = parser.parse_args(
        ["run", "--project", "p", "--config", "c", "--candidate", "cand", "--out", "o"]
    )
    assert parsed.command == "run"

    summary_json = format_cli_summary(
        {"accepted": True, "attempts": [{}], "changed_files": ["pkg/a.py"], "diff": "accepted.diff"},
        tmp_path,
    )
    summary = json.loads(summary_json)
    assert summary["accepted"] is True
    assert summary["attempts"] == 1


def test_run_handles_directory_collision_source_copy_race_and_root_symlink(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    # Candidate path 'pkg' collides with existing directory 'pkg/'
    report = run(
        project,
        make_config(tmp_path, allowlist=["*"]),
        make_candidate(tmp_path, [{"files": {"pkg": "x = 1\n"}}]),
        tmp_path / "out_dir_collision",
    )
    assert report["accepted"] is False
    assert "directory" in report["attempts"][0]["reason"]

    # Candidate parent path 'pkg/mathutil.py/nested.py' collides with existing file 'pkg/mathutil.py'
    report_parent = run(
        project,
        make_config(tmp_path, allowlist=["**"]),
        make_candidate(tmp_path, [{"files": {"pkg/mathutil.py/nested.py": "x = 1\n"}}]),
        tmp_path / "out_parent_collision",
    )
    assert report_parent["accepted"] is False
    assert "cannot write candidate file" in report_parent["attempts"][0]["reason"]

    # Root project symlink check
    with (
        patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == project),
        pytest.raises(RefactorError, match="symlink"),
    ):
        run(
            project,
            make_config(tmp_path),
            make_candidate(tmp_path, [{"files": {"pkg/mathutil.py": "def add(a, b):\n    return a + b\n"}}]),
            tmp_path / "out_symlink",
        )

    # Source changed while copying check
    manifests = [{"pkg/mathutil.py": "hash1"}, {"pkg/mathutil.py": "hash2"}]
    with (
        patch("bounded_refactor.core.file_manifest", side_effect=manifests),
        pytest.raises(RefactorError, match="source changed while copying"),
    ):
        run(
            project,
            make_config(tmp_path),
            make_candidate(tmp_path, [{"files": {"pkg/mathutil.py": "def add(a, b):\n    return a + b\n"}}]),
            tmp_path / "out_copy_race",
        )


def test_execute_check_handles_taskkill_oserror_and_kill_race(tmp_path: Path) -> None:
    def fake_popen(command: Any, cwd: Any, stdout: Any, stderr: Any, **kwargs: Any) -> MagicMock:
        proc = MagicMock()
        proc.pid = 7777
        proc.returncode = None
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd=command, timeout=0.5), 0]
        proc.poll.side_effect = [None, None]
        proc.kill.side_effect = ProcessLookupError("already gone")
        return proc

    with (
        patch("bounded_refactor.core.os.name", "nt"),
        patch("bounded_refactor.core.subprocess.Popen", side_effect=fake_popen),
        patch("bounded_refactor.core.subprocess.run", side_effect=OSError("taskkill unavailable")),
    ):
        result, timed_out = execute_check(["mock-cmd"], tmp_path, 0.5)
    assert timed_out is True
    assert result["returncode"] is None

