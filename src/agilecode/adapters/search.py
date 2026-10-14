"""Bounded lexical, regex, path and Python-AST discovery; no vector service."""
from __future__ import annotations
import ast
import asyncio
import fnmatch
import hashlib
import time
from pathlib import Path
import regex
from agilecode.core.types import AgileCodeError, AccessDenied
from .git import GitRepository
from .workspace import Workspace


class SearchService:
    def __init__(self, workspace: Workspace, git: GitRepository, settings=None):
        self.workspace, self.git = workspace, git
        self.settings = settings

    async def search(self, query: str, kind="text", scope="workspace", path=".", limit=100, case_sensitive=False, cursor=None) -> dict:
        if kind not in {"text", "regex", "path", "symbol", "references"}:
            raise AgileCodeError("Search kind must be text, regex, path, symbol, or references.")
        root = self.workspace.resolve(path, scope)
        base = {"workspace": self.workspace.root, "project": self.workspace.project, "global": self.workspace.global_root}[scope]
        if scope in {"workspace", "project"}:
            paths = [p for p in await self.git.files(base) if self.git.eligible(p, tracked=True)]
        else:
            listing = await asyncio.to_thread(self.workspace.list_files, str(root), scope, True, None)
            paths = [p["path"] for p in listing["files"] if not p["directory"]]
        settings = self.settings() if self.settings is not None else None
        return await asyncio.to_thread(
            self._scan, paths, root, base, query, kind, scope, limit, case_sensitive,
            cursor, settings.search_file_budget if settings else None,
            settings.search_time_budget_seconds if settings else None
        )

    def _scan(self, paths, root, base, query, kind, scope, limit, case_sensitive,
              cursor=None, file_budget=None, time_budget=None):
        """Return one complete result page and a cursor for the next page.

        A cursor resumes inside a file rather than silently dropping the
        remainder when the requested result count is reached.
        """
        started = time.monotonic()
        hits, errors = [], []
        scanned = 0
        start_file, start_hit = 0, 0
        if cursor is not None:
            try:
                left, right = cursor.split(":", 1)
                start_file, start_hit = int(left), int(right)
            except (ValueError, AttributeError) as exc:
                raise AgileCodeError("Invalid search continuation cursor.") from exc
            if start_file < 0 or start_hit < 0 or start_file > len(paths):
                raise AgileCodeError("Search cursor is outside the available files.")

        pattern = None
        if kind in {"text", "regex", "references"}:
            expression = query if kind == "regex" else regex.escape(query)
            try:
                pattern = regex.compile(expression, 0 if case_sensitive else regex.IGNORECASE)
            except regex.error as exc:
                raise AgileCodeError(f"Invalid regular expression: {exc}") from exc

        def output(partial=False, next_cursor=None):
            return {
                "matches": hits, "partial": partial,
                "next_cursor": next_cursor, "scanned_files": scanned,
                "errors": errors, "kind": kind, "scope": scope,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }

        for file_index in range(start_file, len(paths)):
            if (file_budget is not None and scanned >= file_budget) or (
                time_budget is not None and time.monotonic() - started >= time_budget
            ):
                return output(True, f"{file_index}:0")
            rel = paths[file_index]
            try:
                file = self.workspace.resolve(rel, scope)
                if root != file and root not in file.parents:
                    continue
                if not file.is_file() or file.is_symlink():
                    continue
                scanned += 1
                hasher = hashlib.sha256()
                with file.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        hasher.update(block)
                file_hash = hasher.hexdigest()

                def matches():
                    if kind == "path":
                        target = rel if case_sensitive else rel.lower()
                        needle = query if case_sensitive else query.lower()
                        if fnmatch.fnmatch(target, needle) or needle in target:
                            yield {"path": rel, "scope": scope, "hash": file_hash, "kind": kind}
                        return

                    if kind == "symbol" and file.suffix == ".py":
                        text = file.read_text(encoding="utf-8-sig")
                        lines = text.splitlines()
                        try:
                            tree = ast.parse(text)
                        except SyntaxError as exc:
                            errors.append({"path": rel, "error": f"Python syntax error: {exc.lineno}"})
                            return
                        for node in ast.walk(tree):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                hay, needle = (node.name, query) if case_sensitive else (node.name.lower(), query.lower())
                                if needle in hay:
                                    yield {"path": rel, "scope": scope, "hash": file_hash,
                                           "kind": "python_symbol", "symbol": node.name,
                                           "symbol_type": type(node).__name__, "start_line": node.lineno,
                                           "end_line": node.end_lineno, "context": lines[node.lineno - 1][:500]}
                        return

                    candidate = regex.compile(r"\\b(class|struct|interface|function|def|fn|func)\\s+(\\w+)") if kind == "symbol" else None
                    with file.open("r", encoding="utf-8-sig") as source:
                        for line_no, line in enumerate(source, 1):
                            if kind == "symbol":
                                for match in candidate.finditer(line, timeout=time_budget):
                                    if query.lower() in match.group(2).lower():
                                        yield {"path": rel, "scope": scope, "hash": file_hash,
                                               "kind": "declaration_candidate", "start_line": line_no,
                                               "end_line": line_no, "context": line[:500]}
                            else:
                                match = pattern.search(line, timeout=time_budget)
                                if match:
                                    yield {"path": rel, "scope": scope, "hash": file_hash,
                                           "kind": "reference_candidate" if kind == "references" else kind,
                                           "start_line": line_no, "end_line": line_no,
                                           "context": line[max(0, match.start() - 100):match.end() + 250]}

                for hit_index, hit in enumerate(matches()):
                    if file_index == start_file and hit_index < start_hit:
                        continue
                    if len(hits) >= limit:
                        return output(True, f"{file_index}:{hit_index}")
                    hits.append(hit)
            except TimeoutError:
                errors.append({"path": rel, "error": "Search time budget exceeded; adjust it in Settings."})
                return output(True, f"{file_index}:0")
            except (AccessDenied, AgileCodeError, OSError, UnicodeError) as exc:
                errors.append({"path": rel, "error": str(exc)})
        return output()
