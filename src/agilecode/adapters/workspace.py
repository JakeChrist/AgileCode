"""Canonical worktree-scoped file access and exact, preconditioned edits."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from agilecode.adapters.document_read import extract_docx_text, extract_pdf_text
from agilecode.core.types import AccessDenied, AgileCodeError, Conflict

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
READ_LIMIT = 256 * 1024
EXCLUDED_DIRS = {".git", ".agilecode", ".agile_code", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build"}
SENSITIVE_NAMES = {".env", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def decode_text(data: bytes) -> tuple[str, str]:
    if b"\x00" in data:
        raise AgileCodeError("This is a binary file; text editing is not supported.")
    encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError as exc:
        raise AgileCodeError("The file is not UTF-8 text. It was not modified.") from exc


class Workspace:
    def __init__(self, root: Path, project: Path, global_root: Path, *, readonly=False):
        self.root = root.resolve()
        self.project = project.resolve()
        self.global_root = global_root.resolve()
        self.readonly = readonly

    def resolve(self, value: str, scope="workspace", *, write=False, allow_leaf_symlink=False) -> Path:
        roots = {"workspace": self.root, "project": self.project, "global": self.global_root}
        if scope not in roots:
            raise AccessDenied("Scope must be workspace, project, or global.")
        if write and (self.readonly or scope != "workspace"):
            raise AccessDenied("Only the owning ticket worktree is writable.")
        if not value or "\x00" in value:
            raise AccessDenied("Invalid path.")
        root = roots[scope]
        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else root / raw
        # Resolve parents separately for operations on a link itself; never follow an
        # existing parent link into another checkout.
        resolved = (candidate.parent.resolve() / candidate.name) if allow_leaf_symlink else candidate.resolve()
        if not inside(resolved, root):
            raise AccessDenied(f"Path is outside the approved {scope} root: {value}")
        relative = resolved.relative_to(root)
        parts = relative.parts
        if ".git" in parts:
            raise AccessDenied("Git administration is available only through engine-owned operations.")
        if ".agile_code" in parts:
            raise AccessDenied("Legacy AgileCode runtime data is not agent context.")
        if scope == "workspace" and ".agilecode" in parts:
            raise AccessDenied("Application internals are not ticket source.")
        if scope == "project" and ".agilecode" in parts:
            allowed = len(parts) >= 2 and parts[:2] == (".agilecode", "skills")
            if not allowed:
                raise AccessDenied("Project runtime data and sibling ticket worktrees are private.")
        if scope == "global" and (any(p in {"artifacts", "worktrees", "integration"} for p in parts) or resolved.suffix in {".db", ".sqlite"}):
            raise AccessDenied("Internal database/runtime files are not agent context.")
        if any(p in SENSITIVE_NAMES or (p.startswith(".env.") and p not in {".env.example", ".env.sample"}) for p in parts):
            raise AccessDenied("Protected credential file; use a credential-free example or settings view.")
        if write and resolved == root:
            raise AccessDenied("The worktree root itself cannot be replaced or deleted.")
        return resolved

    def read(self, path: str, scope="workspace", start_line=1, end_line=None) -> dict[str, Any]:
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise AgileCodeError("Line ranges are one-based and inclusive.")
        target = self.resolve(path, scope)
        if not target.is_file():
            raise AgileCodeError(f"Not a regular readable file: {path}")
        suffix = target.suffix.lower()
        is_document = suffix in {".pdf", ".docx"}
        size_limit = MAX_DOCUMENT_BYTES if is_document else MAX_FILE_BYTES
        size = target.stat().st_size
        if size > size_limit:
            raise AgileCodeError(f"File exceeds the {size_limit // 1024 // 1024} MiB read limit.")
        data = target.read_bytes()
        if len(data) > size_limit:
            raise AgileCodeError(f"File exceeds the {size_limit // 1024 // 1024} MiB read limit.")
        if suffix == ".pdf":
            text, encoding = extract_pdf_text(data), "extracted-text"
        elif suffix == ".docx":
            text, encoding = extract_docx_text(data), "extracted-text"
        else:
            text, encoding = decode_text(data)
        lines = text.splitlines(keepends=True)
        stop = min(end_line or len(lines), len(lines))
        chosen: list[str] = []
        byte_count = 0
        clipped = False
        for line in lines[start_line-1:stop]:
            encoded_size = len(line.encode("utf-8"))
            if byte_count + encoded_size > READ_LIMIT:
                clipped = True
                if not chosen:
                    chosen.append(line.encode("utf-8")[:READ_LIMIT].decode("utf-8", errors="ignore"))
                break
            chosen.append(line)
            byte_count += encoded_size
        result_text = "".join(chosen)
        return {"path": str(target.relative_to({"workspace": self.root, "project": self.project, "global": self.global_root}[scope])),
                "scope": scope, "hash": digest(data), "size": len(data), "encoding": encoding,
                "newline": "CRLF" if "\r\n" in text else "LF", "total_lines": len(lines),
                "start_line": start_line, "end_line": start_line + len(chosen) - 1,
                "partial": start_line > 1 or start_line + len(chosen) - 1 < len(lines) or clipped,
                "content": result_text}

    def list_files(self, path=".", scope="workspace", recursive=False, limit=500) -> dict:
        root = self.resolve(path, scope)
        if not root.is_dir():
            raise AgileCodeError("Not a directory.")
        output = []
        skipped = []
        walk_capped = False
        iterator = root.rglob("*") if recursive else root.iterdir()
        # os.walk prunes excluded directories; rglob would still visit them.
        if recursive:
            paths = []
            for parent, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not (Path(parent)/d).is_symlink())
                paths.extend(Path(parent)/n for n in sorted(names))
                if len(paths) > limit * 4:
                    walk_capped = True
                    break
            iterator = iter(paths)
        base = {"workspace": self.root, "project": self.project, "global": self.global_root}[scope]
        for target in iterator:
            if target.name in EXCLUDED_DIRS:
                continue
            try:
                checked = self.resolve(str(target), scope)
            except AccessDenied:
                skipped.append(target.name)
                continue
            output.append({"path": str(target.relative_to(base)), "directory": checked.is_dir(), "symlink": target.is_symlink()})
            if len(output) >= limit:
                return {"files": output, "partial": True, "skipped": skipped[:20]}
        return {"files": output, "partial": walk_capped, "skipped": skipped[:20]}

    def _save(self, target: Path, data: bytes, expected_hash: str | None, *, create=False) -> dict:
        if len(data) > MAX_FILE_BYTES:
            raise AgileCodeError("Write exceeds the supported text-file size.")
        if target.is_symlink():
            raise AccessDenied("Write the actual in-worktree file, not a symlink.")
        if target.exists():
            if create:
                raise Conflict("Create-only write refused: file already exists.")
            old = target.read_bytes()
            if expected_hash is None or digest(old) != expected_hash:
                raise Conflict("The file changed or its expected hash was omitted. Read it again.")
            mode = stat.S_IMODE(target.stat().st_mode)
        else:
            if not create or expected_hash is not None:
                raise Conflict("The expected file does not exist.")
            old = None
            mode = 0o644
        target.parent.mkdir(parents=True, exist_ok=True)
        self.resolve(str(target), write=True)
        fd, temp = tempfile.mkstemp(prefix=".agilecode-write-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, mode)
            # Recheck immediately before publication, after any approval/wait.
            self.resolve(str(target), write=True)
            if old is not None and (not target.exists() or digest(target.read_bytes()) != expected_hash):
                raise Conflict("File changed while preparing the write.")
            if old is None and target.exists():
                raise Conflict("Another writer created the destination.")
            os.replace(temp, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return {"path": str(target.relative_to(self.root)), "before_hash": digest(old) if old is not None else None,
                "hash": digest(data), "bytes": len(data)}

    def write(self, path: str, content: str, expected_hash: str | None = None, create=False) -> dict:
        target = self.resolve(path, write=True)
        encoding = "utf-8"
        if target.exists():
            original, encoding = decode_text(target.read_bytes())
            if "\r\n" in original:
                content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        return self._save(target, content.encode(encoding), expected_hash, create=create)

    def edit(self, path: str, expected_hash: str, replacements: list[dict]) -> dict:
        target = self.resolve(path, write=True)
        data = target.read_bytes()
        if digest(data) != expected_hash:
            raise Conflict("Stale edit: file bytes no longer match the read hash.")
        text, encoding = decode_text(data)
        crlf = "\r\n" in text
        for replacement in replacements:
            old, new = replacement["old"], replacement["new"]
            if crlf:
                old = old.replace("\r\n", "\n").replace("\n", "\r\n")
                new = new.replace("\r\n", "\n").replace("\n", "\r\n")
            if not old:
                raise AgileCodeError("Empty search strings are not permitted.")
            count = text.count(old)
            if count != replacement.get("expected_count", 1):
                raise Conflict(f"Expected {replacement.get('expected_count', 1)} exact matches; found {count}. No edits applied.")
            text = text.replace(old, new)
        return self._save(target, text.encode(encoding), expected_hash)

    def delete(self, path: str, expected_hash: str) -> dict:
        target = self.resolve(path, write=True)
        if not target.is_file() or target.is_symlink():
            raise AccessDenied("Delete supports regular files, not recursive directories or symlinks.")
        if digest(target.read_bytes()) != expected_hash:
            raise Conflict("File changed before deletion.")
        target.unlink()
        return {"deleted": path, "before_hash": expected_hash}

    def move(self, path: str, destination: str, expected_hash: str) -> dict:
        source, target = self.resolve(path, write=True), self.resolve(destination, write=True)
        if not source.is_file() or source.is_symlink() or target.exists():
            raise Conflict("Move requires a regular source file and an absent destination.")
        if digest(source.read_bytes()) != expected_hash:
            raise Conflict("Source changed before move.")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.resolve(str(target), write=True)
        source.rename(target)
        return {"from": path, "to": destination, "hash": expected_hash}
