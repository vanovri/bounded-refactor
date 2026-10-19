"""Deterministic ZIP for the bounded refactor release."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
ZIP_PATH = ROOT / "dist" / f"bounded-refactor-v{VERSION}.zip"
SUMS_PATH = ROOT / "dist" / f"SHA256SUMS-v{VERSION}.txt"
FIXED = (2026, 10, 19, 0, 0, 0)
FILES = [
    ".gitattributes", ".gitignore", "LICENSE", "README.md", "pyproject.toml",
    "bounded_refactor/__init__.py", "bounded_refactor/cli.py", "bounded_refactor/core.py",
    "fixtures/config.json", "fixtures/candidate.json",
    "fixtures/project/pkg/__init__.py", "fixtures/project/pkg/mathutil.py",
    "tests/test_bounded_refactor.py", "tools/package_release.py",
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build() -> dict:
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit("missing: " + ", ".join(missing))
    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(FILES):
            info = zipfile.ZipInfo(name, date_time=FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (ROOT / name).read_bytes())
    checksums = {name: sha((ROOT / name).read_bytes()) for name in FILES}
    SUMS_PATH.write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())), encoding="utf-8", newline="\n")
    problems = []
    with zipfile.ZipFile(ZIP_PATH) as archive:
        if sorted(archive.namelist()) != sorted(FILES):
            problems.append("entry list differs")
        for name in archive.namelist():
            if sha(archive.read(name)) != checksums[name]:
                problems.append(f"content differs: {name}")
    return {"zip": str(ZIP_PATH.relative_to(ROOT)), "sha256": sha(ZIP_PATH.read_bytes()), "files": len(FILES), "verified": not problems, "problems": problems}


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
