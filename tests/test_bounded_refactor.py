"""Tests for the bounded refactor runner."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from bounded_refactor.core import RefactorError, load_candidate, load_config, run
from bounded_refactor.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def copy_project(tmp_path: Path) -> Path:
    target = tmp_path / "project"
    shutil.copytree(FIXTURES / "project", target)
    return target


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def config(tmp_path: Path, **overrides) -> Path:
    payload = json.loads((FIXTURES / "config.json").read_text(encoding="utf-8"))
    payload.update(overrides)
    return write_json(tmp_path / "config.json", payload)


def candidate(tmp_path: Path, attempts: list[dict]) -> Path:
    return write_json(tmp_path / "candidate.json", {"attempts": attempts})


def fix_attempt() -> dict:
    return {"files": {"pkg/mathutil.py": "def add(a, b):\n    return a + b\n"}}


def test_accepts_a_verified_refactor(tmp_path):
    project = copy_project(tmp_path)
    report = run(project, config(tmp_path), candidate(tmp_path, [fix_attempt()]), tmp_path / "out")
    assert report["accepted"] is True
    assert report["changed_files"] == ["pkg/mathutil.py"]
    assert report["attempts"][0]["checks"][0]["returncode"] == 0
    diff = (tmp_path / "out" / "accepted.diff").read_text(encoding="utf-8")
    assert "return a + b" in diff
    assert "a/pkg/mathutil.py" in diff


def test_original_project_is_never_modified(tmp_path):
    project = copy_project(tmp_path)
    before = hashlib.sha256((project / "pkg" / "mathutil.py").read_bytes()).hexdigest()
    run(project, config(tmp_path), candidate(tmp_path, [fix_attempt()]), tmp_path / "out")
    after = hashlib.sha256((project / "pkg" / "mathutil.py").read_bytes()).hexdigest()
    assert before == after
    originals = tmp_path / "out" / "originals" / "pkg" / "mathutil.py"
    assert originals.exists()
    assert "a - b" in originals.read_text(encoding="utf-8")


def test_rejects_paths_outside_the_allowlist(tmp_path):
    project = copy_project(tmp_path)
    attempt = {"files": {"pkg/mathutil.py": "def add(a, b):\n    return a + b\n", "setup.py": "print('no')\n"}}
    report = run(project, config(tmp_path), candidate(tmp_path, [attempt]), tmp_path / "out")
    assert report["accepted"] is False
    assert "allowlist" in report["attempts"][0]["reason"]


def test_rejects_traversal_paths(tmp_path):
    project = copy_project(tmp_path)
    attempt = {"files": {"../evil.py": "print('no')\n"}}
    report = run(project, config(tmp_path), candidate(tmp_path, [attempt]), tmp_path / "out")
    assert report["accepted"] is False
    assert "unsafe" in report["attempts"][0]["reason"]


def test_rejects_removed_public_api(tmp_path):
    project = copy_project(tmp_path)
    attempt = {"files": {"pkg/mathutil.py": "def subtract(a, b):\n    return a - b\n"}}
    report = run(project, config(tmp_path), candidate(tmp_path, [attempt]), tmp_path / "out")
    assert report["accepted"] is False
    assert "removed public symbols" in report["attempts"][0]["reason"]


def test_rejects_a_failing_check(tmp_path):
    project = copy_project(tmp_path)
    attempt = {"files": {"pkg/mathutil.py": "def add(a, b):\n    return a * b\n"}}
    report = run(project, config(tmp_path), candidate(tmp_path, [attempt]), tmp_path / "out")
    assert report["accepted"] is False
    assert "exit code" in report["attempts"][0]["reason"]


def test_timeout_is_bounded(tmp_path):
    project = copy_project(tmp_path)
    slow = ["python", "-c", "import time; time.sleep(2)"]
    report = run(project, config(tmp_path, check_command=slow, timeout_seconds=0.25), candidate(tmp_path, [fix_attempt()]), tmp_path / "out")
    assert report["accepted"] is False
    assert "exceeded" in report["attempts"][0]["reason"]


def test_attempt_limit_is_enforced(tmp_path):
    project = copy_project(tmp_path)
    attempts = [
        {"files": {"pkg/mathutil.py": "def add(a, b):\n    return a * b\n"}},
        {"files": {"pkg/mathutil.py": "def add(a, b):\n    return a - b\n"}},
        fix_attempt(),
    ]
    report = run(project, config(tmp_path, max_attempts=2), candidate(tmp_path, attempts), tmp_path / "out")
    assert len(report["attempts"]) == 2
    assert report["accepted"] is False


def test_repairgate_gate_accepts_a_passed_report(tmp_path):
    project = copy_project(tmp_path)
    gate = write_json(tmp_path / "gate.json", {
        "version": "0.2.0", "status": "passed", "provider": "recorded proposals",
        "cases": [{"status": "accepted", "source_digest": "abc"}],
    })
    report = run(project, config(tmp_path), candidate(tmp_path, [fix_attempt()]), tmp_path / "out", gate)
    assert report["repairgate"]["used"] is True
    assert report["repairgate"]["accepted_cases"] == 1
    assert report["repairgate"]["source_digests"] == ["abc"]


def test_repairgate_gate_rejects_a_failed_report(tmp_path):
    project = copy_project(tmp_path)
    gate = write_json(tmp_path / "gate.json", {"status": "failed", "cases": []})
    with pytest.raises(RefactorError, match="not 'passed'"):
        run(project, config(tmp_path), candidate(tmp_path, [fix_attempt()]), tmp_path / "out", gate)


def test_config_validation(tmp_path):
    bad = write_json(tmp_path / "bad.json", {"allowlist": [], "check_command": ["python"]})
    with pytest.raises(RefactorError, match="allowlist"):
        load_config(bad)
    bad = write_json(tmp_path / "bad2.json", {"allowlist": ["*.py"], "check_command": []})
    with pytest.raises(RefactorError, match="check_command"):
        load_config(bad)
    bad = write_json(tmp_path / "bad3.json", {"allowlist": ["*.py"], "check_command": ["python"], "max_attempts": 99})
    with pytest.raises(RefactorError, match="max_attempts"):
        load_config(bad)
    bad = write_json(tmp_path / "bad4.json", {"allowlist": ["*.py"], "check_command": ["python"], "timeout_seconds": 999})
    with pytest.raises(RefactorError, match="timeout_seconds"):
        load_config(bad)


def test_candidate_validation(tmp_path):
    bad = write_json(tmp_path / "bad.json", {"attempts": []})
    with pytest.raises(RefactorError, match="attempts"):
        load_candidate(bad)
    bad = write_json(tmp_path / "bad2.json", {"attempts": [{"files": {"x": 1}}]})
    with pytest.raises(RefactorError, match="strings"):
        load_candidate(bad2 := bad)


def test_cli_exit_codes(tmp_path):
    project = copy_project(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    code = main(["run", "--project", str(project), "--config", str(config(tmp_path)), "--candidate", str(candidate(tmp_path, [fix_attempt()])), "--out", str(out)])
    assert code == 0
    assert (out / "report.json").exists()
    bad = candidate(tmp_path, [{"files": {"pkg/mathutil.py": "def add(a, b):\n    return a * b\n"}}])
    assert main(["run", "--project", str(project), "--config", str(config(tmp_path)), "--candidate", str(bad), "--out", str(out)]) == 1
