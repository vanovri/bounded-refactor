# Refactor & Verification Changelog (`bounded-refactor`)

## 1. Branch & Git Safety Summary

| Attribute | Value |
| --- | --- |
| Original Branch | `main` (`0881993`) |
| Refactor Branch | `codex/auto-refactor-and-tests` |
| Stash Snapshot | `stash@{0}: On main: pre-refactor-backup-snapshot` (preserves pre-existing untracked `ARCHITECTURE.md`, `TECH_DEBT.md`, `graphify-out/`, and `tests/test_retry_pipeline.py`) |
| Backward Compatibility | 100% preserved (`bounded_refactor.core`, `bounded_refactor.cli:main`, CLI flags, exit codes `0`/`1`/`2`, and `BOUNDED_REFACTOR_V1` report schema) |

---

## 2. Files Modified & Added

| File | Action | Purpose |
| --- | --- | --- |
| `bounded_refactor/__init__.py` | Modified | Added explicit `__all__` exports (`RefactorError`, `run`, `__version__`) and PEP 563 type annotations. |
| `bounded_refactor/core.py` | Modified | Added strict `TypedDict`/`dataclass` contracts, explicit bound validators, modular decomposition of >50-line routines, `O(N)` incremental alias tracking, `os.walk` directory pruning, streaming `hashlib.file_digest`, and structured `OSError`/`UnicodeDecodeError`/`TimeoutExpired` resilience with `logging`. |
| `bounded_refactor/cli.py` | Modified | Extracted `build_parser()` and `format_cli_summary()`, widened `argv` type contract to `Sequence[str] | None`, and added structured `logging` diagnostics. |
| `tests/test_comprehensive_unit.py` | Added | Added 83 deterministic, 100% offline unit & characterization test cases covering retry loops, AST signatures, path/alias security, `bool`/`NaN`/`Inf` bounds, mocked subprocess cleanup (Windows & POSIX), symlink/collision guards, and CLI exit codes. |
| `tests/test_bounded_refactor.py` | Modified | Sorted imports and removed unused walrus variable assignment (`F841`/`I001`) without altering any test assertions. |
| `tests/test_audit_regressions.py` | Modified | Removed unused imports and added explicit `check=False` to `subprocess.run` (`F401`/`I001`/`PLW1510`) without altering any test assertions. |
| `REFACTOR_CHANGELOG.md` | Added | Documented architectural improvements, bug fixes, test/coverage metrics, and static verification commands. |

---

## 3. Architectural & Code Quality Improvements

### Pillar 1: Type Safety & Contracts
- Defined explicit `TypedDict` schemas (`ConfigData`, `CandidateAttempt`, `ManifestEntry`, `CheckResult`, `RepairGateSummary`, `AttemptReportEntry`, `LimitsSummary`, `RunReport`) and `AttemptResult.to_report_entry()` across `bounded_refactor/core.py`.
- Added reusable contract validators (`validate_max_attempts`, `validate_timeout_seconds`, `validate_check_command`) enforcing rejection of `bool`-as-`int` traps (`True`/`False`), non-finite floats (`NaN`, `Inf`, `-Inf`), empty/null-byte argv entries, and non-string candidate `note` fields in both config loading and direct `execute_check()` calls.

### Pillar 2: Modularity & Clean Architecture
- Decomposed the 114-line monolithic `run()` function into focused single-responsibility helpers (`_validate_project_and_output`, `_validate_attempt_paths`, `_apply_candidate_files`, `_run_workspace_check`, `_collect_attempt_diff`, `_evaluate_attempt_in_workspace`, `_evaluate_attempt`, `_build_run_report`).
- Decomposed `public_api()` into `_format_callable_signature()`, `_extract_module_symbols()`, `_is_dunder_all_node()`, and `_extract_dunder_all()`, supporting both plain (`__all__ = [...]`) and type-annotated (`__all__: list[str] = [...]`) export declarations.
- Decomposed `execute_check()` into `_terminate_process_tree()` and `_read_stream_tail()`, and split `cli.main()` into `build_parser()`, `format_cli_summary()`, and `main()`.

### Pillar 3: Performance & Resource Efficiency
- **Incremental Alias Detection (`O(N)` vs `O(N^2)`):** Replaced repeated `{key.casefold() for key in files}` set rebuilds per entry in `load_candidate()` with an incremental `seen: set[str]` over canonicalized POSIX paths.
- **Directory-Pruned Manifest Traversal:** Replaced full-tree `Path.rglob("*")` post-filtering in `file_manifest()` with `os.walk()` in-place `dirnames[:]` pruning so `.git`, `__pycache__`, and `.pytest_cache` subtrees are never entered.
- **Streaming C-Level Hashing & Pre-Compiled Constants:** Upgraded `sha256()` to `hashlib.file_digest(handle, "sha256")` and hoisted `SHA256_HEX_RE`, `EXCLUDED_MANIFEST_DIRS`, `PROTECTED_DIRS`, and `PROTECTED_FILES` to module-level constants.

### Pillar 4: Error Resilience & Concrete Bugs Fixed
1. **Unhandled `OSError` / `UnicodeDecodeError` in Config & Candidate Loaders:** Previously, missing files or invalid UTF-8 bytes in `--config` or `--candidate` bypassed `RefactorError` and crashed the CLI instead of exiting cleanly with code `2`. Unified JSON loading in `_read_json_file()` to convert `OSError`, `UnicodeDecodeError`, and `json.JSONDecodeError` into `RefactorError`.
2. **Case-Insensitive Bypass & Empty-Path `IndexError` in `protected()`:** Previously, `protected("")` raised `IndexError: tuple index out of range`, and uppercase/mixed-case variants (`PyProject.toml`, `pkg/CONFTEST.PY`, `.GIT/HEAD`, `.GitHub/workflows/...`, `pkg/TEST_FOO.PY`) bypassed protection on case-insensitive filesystems (Windows/macOS). Hardened `protected()` with empty-part guards and `.casefold()` matching.
3. **Canonical Alias & Relative Path Normalization Bypass:** Previously, `./pkg/mathutil.py` vs `pkg/mathutil.py` and `pkg//mathutil.py` vs `pkg/mathutil.py` bypassed duplicate alias detection, and trailing slashes or leading/trailing spaces passed `relative_is_safe()`. Canonicalized alias keys via `PurePosixPath` and tightened segment validation in `relative_is_safe()`.
4. **RepairGate Non-Object & Null-Digest Type Crashes:** Fixed `AttributeError` when RepairGate JSON root is not an object (`[]`), `TypeError` in `re.fullmatch` when `source_digest` is `null`, and falsy `cases: false` masking in `repairgate_gate()`.
5. **Workspace Collision, Root Symlink, & Process-Tree Cleanup Races:** Added pre-resolve root symlink check, candidate file-vs-directory collision handling in `_apply_candidate_files()`, cross-platform `SIGKILL`/`SIGTERM` resolution, and `OSError`/`ProcessLookupError` guards around `taskkill` and `process.kill()` in `_terminate_process_tree()`, backed by `logging.getLogger(__name__)`.

---

## 4. Before vs. After Test Suite & Coverage Metrics

| Module | Before Stmts | Before Miss | Before Cover | After Stmts | After Miss | After Cover |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `bounded_refactor/__init__.py` | 1 | 0 | 100% | 4 | 0 | 100% |
| `bounded_refactor/cli.py` | 25 | 4 | 84% | 35 | 1 | 97% |
| `bounded_refactor/core.py` | 282 | 38 | 87% | 462 | 0 | 100% |
| **TOTAL** | **308** | **42** | **86%** | **501** | **1** | **99%** |

- **Test Count:** `28 passed` (100% pass rate) -> **`111 passed` (100% pass rate, +83 new offline unit & characterization tests)**

### Verification Commands & Exit Codes

| Check | Command | Exit Code |
| --- | --- | ---: |
| Unit Tests + Coverage | `PYTHONPATH=. <QA_PYTHON> -m pytest --cov=bounded_refactor --cov-report=term-missing` | `0` |
| Ruff Linter | `<QA_VENV>\Scripts\ruff.exe check bounded_refactor tests` (and `ruff.exe check .`) | `0` |
| Mypy Type Checker | `<QA_VENV>\Scripts\mypy.exe bounded_refactor --ignore-missing-imports` | `0` |
