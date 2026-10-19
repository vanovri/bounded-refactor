# Bounded refactor runner

Apply an agent-proposed refactor to a **local copy**, under explicit limits, and only export a diff if the checks and the public API survive.

```sh
bounded-refactor run \
  --project ./my-project \
  --config refactor.json \
  --candidate candidate.json \
  --out ./review \
  --repairgate-report repairgate.json
```

The original tree is never modified. The accepted change is a diff plus a report; applying it is a separate, human decision.

## Safety model

| Boundary | Behaviour |
| --- | --- |
| Trusted input only | The project must be local and symlink-free; the check command is an argv list, never a shell string. |
| Allowlist | Every candidate path must match `config.allowlist` globs; `..`, absolute paths and symlinks are rejected. |
| Original preservation | A full copy is written to `out/originals/` with a SHA-256 manifest before anything runs. |
| Attempt limit | `config.max_attempts` (1â€“5); later attempts are ignored. |
| Time limit | Each check runs with `timeout_seconds` (max 120) and its live process tree is stopped on timeout. |
| API check | Public top-level names (or `__all__`) in `api_paths` are compared before/after; removed or renamed symbols reject the attempt. |
| RepairGate gate | With `--repairgate-report`, the report must be `status: passed` and contain at least one accepted case; its digests are recorded. |
| Output | `accepted.diff`, `report.json`, `originals/` and `originals-manifest.json`. Rejected attempts produce an empty diff. |

The runner executes the configured check command in a temporary copy. It does not fetch anything, does not run the candidate's own code beyond that explicit command, and does not install dependencies.

## Config

```json
{
  "allowlist": ["pkg/*.py"],
  "check_command": ["python", "-c", "from pkg.mathutil import add; assert add(2, 3) == 5"],
  "api_paths": ["pkg/mathutil.py"],
  "max_attempts": 2,
  "timeout_seconds": 10
}
```

## Candidate

Full file contents, not a patch. This removes ambiguity about hunk offsets and makes every write explicit.

```json
{
  "attempts": [
    {
      "note": "fix the arithmetic, keep the public API",
      "files": { "pkg/mathutil.py": "def add(a, b):\n    return a + b\n" }
    }
  ]
}
```

## RepairGate integration

RepairGate separates a proposal from the acceptance decision. This runner treats a RepairGate report as a **precondition**, not as proof that this refactor is correct:

- `status` must be `passed`;
- at least one case must be `accepted`;
- accepted `source_digest` values are copied into `report.json` for traceability.

The runner does not re-run RepairGate and does not claim its independent checks passed for this change.

## Report

```json
{
  "accepted": true,
  "attempts": [{"number": 1, "status": "accepted", "changed": ["pkg/mathutil.py"], "checks": [{"returncode": 0}]}],
  "changed_files": ["pkg/mathutil.py"],
  "diff": "accepted.diff",
  "repairgate": {"used": true, "accepted_cases": 1, "source_digests": ["..."]}
}
```

Exit codes: `0` accepted, `1` rejected, `2` invalid input or gate failure.

## Verification

27 tests cover acceptance, original preservation, allowlist rejection, traversal rejection, API removal, failing checks, timeouts, attempt limits, RepairGate pass/fail, config and candidate validation, and CLI exit codes.

Fixture: a broken `add(a, b)` that returns `a - b`; the accepted candidate restores `a + b` while keeping `add` public.

## Limits

- One process, one project, one check command. No test discovery, coverage, lint matrix or package install.
- The API check is Python-AST-only; other languages need their own comparator.
- A passing check command is evidence for that command only.
- The diff is unified text; it is not applied automatically.
- Original preservation is a copy with before/after hash comparisons, not an atomic filesystem snapshot.
- No remote or multi-user execution; run it on a machine you trust.

## Development

```sh
python -m pytest -q
python -m bounded_refactor.cli run --project fixtures/project --config fixtures/config.json --candidate fixtures/candidate.json --out /tmp/review
```

MIT â€” see [LICENSE](LICENSE).

## Audit corrections in 0.2.0

- Tests, CI and project test configuration remain protected even under a broad candidate allowlist.
- Freeze the copied source and tests before each check. A check that changes them rejects the candidate. Python cache directories are excluded from this comparison.
- Check public function argument/default signatures and public class methods in addition to exported names. This remains an AST surface check, not semantic API compatibility proof.
- Reject POSIX/Windows absolute paths, drive-relative paths, alternate streams and traversal on either platform.
- Use the preserved snapshot for every attempt. Reject overlapping source/output directories and nonempty output directories. Never erase earlier evidence.
- Detect source changes during verification. Correctly replay diffs for files without final newlines.
- Kill the check's process group on POSIX and the live process tree on Windows timeout. On Windows, a detached child whose parent already exited is outside this lifetime guarantee.
- Record the SHA-256 of an optional external RepairGate regression report. Its accepted cases must include source digests and preservation evidence. It does not certify the current candidate.

This executes trusted local code with the user's normal OS permissions. It is not a security sandbox. The check and imported candidate may access the network, environment and filesystem. No network-denial or hostile-code confinement is claimed.
