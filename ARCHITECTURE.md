# Architecture: bounded-refactor

Crosscheck: 2026-11-08 WIB. The repository-local offline checks completed with exit code 0 in an interpreter/environment containing the declared dependencies. Hardware, live providers, deployment and unexecuted CI platforms are separate evidence boundaries.

Reviewed on 2026-11-07 against the local working tree (`.`). `graphify-out/graph.json` provides the local AST symbol graph.

## 1. System Context & Data Flow

Bounded candidate-replacement evaluator that verifies file allowlists, public Python AST signatures, subprocess check timeouts, and frozen test/source SHA-256 manifests inside isolated temporary copies before exporting `accepted.diff` and `report.json`.

```mermaid
graph TD
Dev["Developer / CI"] --> CLI["bounded_refactor.cli"]
    CLI --> Validate["Config & Candidate Path Guards (core.py)"]
    Gate["Optional RepairGate Report"] --> Validate
    Validate --> Orig["Original Project + Frozen SHA-256 Manifest"]
    Orig --> Temp["Per-Attempt Temporary Copy"]
    Temp --> AST["Public API AST Signature Check"]
    AST --> Subproc["Time-Bounded Check Subprocess"]
    Subproc --> Freeze["Frozen File & Source Hash Re-verification"]
    Freeze --> Out["accepted.diff + report.json"]
```

## 2. Execution & Pipeline Sequence

```mermaid
sequenceDiagram
participant C as cli.py
    participant R as core.run()
    participant FS as Temp Workspace
    participant P as Check Subprocess
    C->>R: run(project, config, candidates, output)
    R->>FS: Snapshot original project & compute baseline public AST API
    loop Up to max_attempts
      R->>FS: Copy fresh workspace & write allowlisted candidate files
      R->>R: check_api(baseline_ast, candidate_ast)
      R->>P: Spawn check argv with bounded timeout & temp log files
      P-->>R: Exit code + bounded stdout/stderr tails
      R->>FS: Verify non-allowlisted files unchanged
      alt Public API intact & check exit 0
        R->>FS: Export unified accepted.diff & report.json
      else Rejected
        R->>R: Record rejection reason and continue
      end
    end
    R->>FS: Verify original project tree SHA-256 untouched
```

## 3. Module Breakdown

| Path | Responsibility |
| :--- | :--- |
| `bounded_refactor/core.py` | Config/candidate schema & path-traversal validation, streaming `file_digest` SHA-256 manifest hashing, AST public signature extraction (`__all__`, sync/async functions, classes), process-tree timeout cleanup, and diff/report export. |
| `bounded_refactor/cli.py` | CLI argument parser and exit-code mapper (`0` accepted, `1` rejected, `2` config/input error). |
| `fixtures/project` | Synthetic broken arithmetic package (`pkg/mathutil.py`) and test suite used for deterministic local verification. |
| `tests/test_bounded_refactor.py` | Core acceptance, path-escape, API-breakage, timeout, RepairGate precondition, and source-immutability tests. |
| `tests/test_audit_regressions.py` | Regression coverage for encoding, symlink/path alias handling, and boundary conditions. |
| `tests/test_comprehensive_unit.py` | Additional unit and pipeline boundary coverage. |

## 4. State Management & Failure Handling

Original repository files are never modified in place: `run()` copies originals into a preserved snapshot and creates a fresh `TemporaryDirectory` per candidate attempt. Both before and after verification, `file_manifest()` hashes the original tree to detect any out-of-band mutation.

## 5. Verification Commands

```powershell
Set-Location '.'
& 'C:\Python314\python.exe' -m pytest -q
```

## 6. Integration Boundaries

Executes configured local check commands with current-user OS permissions; temporary directory isolation is an audit boundary rather than a kernel sandbox.
