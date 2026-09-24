"""Bounded lexical, regex, path and Python-AST discovery; no vector service."""
from __future__ import annotations
import ast
import asyncio
import fnmatch
import time
from pathlib import Path
from typing import Callable
import regex
from agilecode.core.types import AgileCodeError, AccessDenied, Settings
from .git import GitRepository
from .workspace import Workspace, digest, decode_text


class SearchService:
    def __init__(self, workspace: Workspace, git: GitRepository,
                 settings: Callable[[], Settings] | None = None):
        self.workspace, self.git, self.settings = workspace, git, settings

    async def search(self, query: str, kind="text", scope="workspace", path=".", limit=100, case_sensitive=False) -> dict:
        if kind not in {"text", "regex", "path", "symbol", "references"}:
            raise AgileCodeError("Search kind must be text, regex, path, symbol, or references.")
        root = self.workspace.resolve(path, scope)
        base = {"workspace": self.workspace.root, "project": self.workspace.project, "global": self.workspace.global_root}[scope]
        if scope in {"workspace", "project"}:
            paths = [p for p in await self.git.files(base) if self.git.eligible(p, tracked=True)]
        else:
            list_limit = self.settings().search_max_files if self.settings else None
            listing = await asyncio.to_thread(
                self.workspace.list_files, str(root), scope, True,
                list_limit if list_limit is not None else max(limit, 1_000_000)
            )
            paths = [p["path"] for p in listing["files"] if not p["directory"]]
        return await asyncio.to_thread(self._scan, paths, root, base, query, kind, scope, limit, case_sensitive)

    def _scan(self, paths, root, base, query, kind, scope, limit, case_sensitive):
        start = time.monotonic()
        hits, errors = [], []
        partial = False
        scanned = 0
        pattern = None
        if kind in {"text", "regex", "references"}:
            expression = query if kind == "regex" else regex.escape(query)
            try:
                pattern = regex.compile(expression, 0 if case_sensitive else regex.IGNORECASE)
            except regex.error as exc:
                raise AgileCodeError(f"Invalid regular expression: {exc}") from exc
        settings = self.settings() if self.settings else None
        max_files = settings.search_max_files if settings else None
        timeout_seconds = settings.search_timeout_seconds if settings else None
        max_file_bytes = self.workspace.max_file_bytes()
        for rel in paths:
            if ((max_files is not None and scanned >= max_files)
                    or (timeout_seconds is not None and time.monotonic() - start > timeout_seconds)
                    or len(hits) >= limit):
                partial = True
                break
            try:
                file = self.workspace.resolve(rel, scope)
                if root != file and root not in file.parents:
                    continue
                if not file.is_file() or file.is_symlink():
                    continue
                if max_file_bytes is not None and file.stat().st_size > max_file_bytes:
                    continue
                scanned += 1
                data = file.read_bytes()
                hash_ = digest(data)
                if kind == "path":
                    target = rel if case_sensitive else rel.lower()
                    needle = query if case_sensitive else query.lower()
                    if fnmatch.fnmatch(target, needle) or needle in target:
                        hits.append({"path": rel, "scope": scope, "hash": hash_, "kind": kind})
                    continue
                text, _ = decode_text(data)
                lines = text.splitlines()
                if kind == "symbol" and file.suffix == ".py":
                    try:
                        tree = ast.parse(text)
                    except SyntaxError as exc:
                        errors.append({"path": rel, "error": f"Python syntax error: {exc.lineno}"})
                        continue
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            hay, needle = (node.name, query) if case_sensitive else (node.name.lower(), query.lower())
                            if needle in hay:
                                hits.append({"path": rel, "scope": scope, "hash": hash_, "kind": "python_symbol",
                                             "symbol": node.name, "symbol_type": type(node).__name__, "start_line": node.lineno,
                                             "end_line": node.end_lineno, "context": lines[node.lineno-1][:500]})
                    continue
                if kind == "symbol":
                    # Non-Python declarations are deliberately labeled candidates.
                    candidate = regex.compile(r"\b(class|struct|interface|function|def|fn|func)\s+(\w+)")
                    for index, line in enumerate(lines):
                        for match in candidate.finditer(line):
                            if query.lower() in match.group(2).lower():
                                hits.append({"path": rel, "scope": scope, "hash": hash_, "kind": "declaration_candidate",
                                             "start_line": index+1, "end_line": index+1, "context": line[:500]})
                    continue
                assert pattern is not None
                for index, line in enumerate(lines):
                    match = pattern.search(line)
                    if match:
                        hits.append({"path": rel, "scope": scope, "hash": hash_,
                                     "kind": "reference_candidate" if kind == "references" else kind,
                                     "start_line": index+1, "end_line": index+1, "context": line[max(0, match.start()-100):match.end()+250]})
                    if len(hits) >= limit:
                        partial = True; break
            except (AccessDenied, AgileCodeError, OSError) as exc:
                errors.append({"path": rel, "error": str(exc)})
        return {"matches": hits[:limit], "partial": partial or len(hits)>limit, "scanned_files": scanned,
                "errors": errors[:30], "kind": kind, "scope": scope, "elapsed_seconds": round(time.monotonic()-start, 3)}
