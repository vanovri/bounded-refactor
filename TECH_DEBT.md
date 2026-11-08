# Technical Debt & Performance Ledger: bounded-refactor

Crosschecked 2026-11-08 WIB. All code diffs below are proposal-only and have not changed production source. Source references and removed patch lines were checked against this working tree. Runtime speedups remain unmeasured unless evidence is explicitly cited.

Reviewed on 2026-11-08 WIB against the current working tree on disk. All line numbers are 1-based and verified directly against the current production files. Every diff below is a non-destructive engineering proposal, not an applied production edit.

## TD-01 — Exclude `.git`, `__pycache__`, and `.pytest_cache` when copying and hashing `snapshot()`

- **Location**: `bounded_refactor/core.py:268-273`
- **Analysis**: While `file_manifest()` prunes `EXCLUDED_MANIFEST_DIRS` (`__pycache__`, `.pytest_cache`, `.git`), `snapshot()` copies the entire `.git` object store and bytecode caches into `output/originals/` and hashes every copied file via `target.rglob('*')`.

```diff
diff --git a/bounded_refactor/core.py b/bounded_refactor/core.py
--- a/bounded_refactor/core.py
+++ b/bounded_refactor/core.py
@@ -268,7 +268,12 @@
 def snapshot(project: Path, target: Path) -> list[ManifestEntry]:
     if target.exists():
         raise RefactorError("snapshot destination already exists")
-    shutil.copytree(project, target, symlinks=False)
+    shutil.copytree(
+        project,
+        target,
+        symlinks=False,
+        ignore=shutil.ignore_patterns(*EXCLUDED_MANIFEST_DIRS),
+    )
     manifest: list[ManifestEntry] = []
     for path in sorted(target.rglob("*")):
         if path.is_file():
```

- **Validation**: Verify `snapshot()` on a git-initialized fixture excludes `.git/` and `__pycache__/` while preserving all tracked source/test files; run `C:\Python314\python.exe -m pytest -q`.

## TD-02 — Include return-annotation parity in public class-method AST signatures (`_extract_module_symbols`)

- **Location**: `bounded_refactor/core.py:305-308`
- **Analysis**: Top-level functions use `_format_callable_signature(node)` (which captures both `args` and `returns`), whereas public class methods drop `member.returns`, allowing a candidate refactor to silently change or strip public method return annotations.

```diff
diff --git a/bounded_refactor/core.py b/bounded_refactor/core.py
--- a/bounded_refactor/core.py
+++ b/bounded_refactor/core.py
@@ -304,8 +304,7 @@
             names.append(node.name)
             for member in node.body:
                 if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
-                    args_dump = ast.dump(member.args, include_attributes=False)
-                    names.append(f"{node.name}.{member.name}:{args_dump}")
+                    names.append(f"{node.name}.{_format_callable_signature(member)}")
         elif isinstance(node, ast.Assign):
             for target in node.targets:
                 if isinstance(target, ast.Name) and not target.id.startswith("_"):
```

- **Validation**: Add a class-method return-annotation change test in `tests/test_retry_pipeline.py` and run `C:\Python314\python.exe -m pytest -q`.
