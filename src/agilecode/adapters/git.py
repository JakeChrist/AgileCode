"""Typed Git/worktree/snapshot operations. No shell command interpolation."""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from agilecode.core.types import AgileCodeError, Conflict, uid
from .workspace import EXCLUDED_DIRS, SENSITIVE_NAMES, inside


@dataclass
class GitResult:
    code: int
    stdout: bytes
    stderr: bytes
    @property
    def text(self):
        return self.stdout.decode("utf-8", errors="replace").strip()


def clean_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C.UTF-8"})
    if extra:
        env.update(extra)
    return env


class GitRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.admin_lock = asyncio.Lock()
        self.operation_timeout = None

    async def run(self, *args: str, cwd: Path | None = None, input: bytes | None = None,
                  env: dict[str, str] | None = None, check=True, timeout=None) -> GitResult:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd or self.root), *args,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=clean_environment(env), start_new_session=True)
        try:
            effective_timeout = timeout if timeout is not None else (self.operation_timeout() if self.operation_timeout else None)
            stdout, stderr = await asyncio.wait_for(process.communicate(input), effective_timeout)
        except BaseException:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        result = GitResult(process.returncode or 0, stdout, stderr)
        if check and result.code:
            raise AgileCodeError(f"Git {args[0] if args else ''} failed: {stderr.decode(errors='replace').strip()}")
        return result

    async def identify(self) -> dict[str, str]:
        top = Path((await self.run("rev-parse", "--show-toplevel")).text).resolve()
        if top != self.root:
            raise AgileCodeError(f"Select the repository root: {top}")
        common = (await self.run("rev-parse", "--path-format=absolute", "--git-common-dir")).text
        # Managed worktrees are not original projects, including detached integration trees.
        git_dir = (await self.run("rev-parse", "--path-format=absolute", "--git-dir")).text
        if Path(common).resolve() != Path(git_dir).resolve():
            raise AgileCodeError("Open the original project checkout, not a linked ticket worktree.")
        return {"root": str(top), "common_git_dir": common}

    async def head(self, cwd: Path | None = None) -> str:
        return (await self.run("rev-parse", "HEAD", cwd=cwd)).text

    async def branch(self, cwd: Path | None = None) -> str:
        result = await self.run("symbolic-ref", "--short", "HEAD", cwd=cwd, check=False)
        if result.code:
            raise AgileCodeError("The original project is in detached HEAD state. Check out the intended base branch.")
        return result.text

    async def status(self, cwd: Path | None = None) -> str:
        return (await self.run("status", "--porcelain=v1", "--untracked-files=all", cwd=cwd)).text

    async def dirty(self, cwd: Path | None = None) -> bool:
        return bool(await self.status(cwd))

    async def ensure_ignore(self) -> bool:
        path = self.root / ".gitignore"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if ".agilecode/" in {line.strip().lstrip('/') for line in text.splitlines()}:
            return False
        with path.open("a", encoding="utf-8") as f:
            f.write(("\n" if text and not text.endswith("\n") else "") + ".agilecode/\n")
        return True

    async def add_worktree(self, path: Path, branch: str | None, base_oid: str) -> None:
        async with self.admin_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                listed = await self.worktrees()
                if str(path.resolve()) in listed:
                    actual = await self.head(path)
                    if actual == base_oid and (branch is None or await self.branch(path) == branch):
                        return
                raise Conflict("Worktree destination already exists; it was not overwritten.")
            args = ["worktree", "add"]
            args += ["-b", branch] if branch else ["--detach"]
            await self.run(*args, str(path), base_oid)

    async def worktrees(self) -> dict[str, dict]:
        text = (await self.run("worktree", "list", "--porcelain")).text
        result: dict[str, dict] = {}
        current: dict[str, str] = {}
        for line in text.splitlines() + [""]:
            if not line:
                if "worktree" in current:
                    result[str(Path(current["worktree"]).resolve())] = current
                current = {}
            else:
                key, _, value = line.partition(" ")
                current[key] = value
        return result

    async def verify_worktree(self, path: Path, branch: str | None = None) -> None:
        trees = await self.worktrees()
        if str(path.resolve()) not in trees:
            raise Conflict("The ticket worktree is missing or no longer belongs to this repository.")
        if branch is not None and trees[str(path.resolve())].get("branch") != f"refs/heads/{branch}":
            raise Conflict("The ticket branch changed outside AgileCode.")

    async def files(self, cwd: Path) -> list[str]:
        data = (await self.run("ls-files", "-z", "--cached", "--others", "--exclude-standard", cwd=cwd)).stdout
        return sorted(set(p.decode("utf-8", errors="surrogateescape") for p in data.split(b"\0") if p))

    @staticmethod
    def eligible(path: str, tracked=False) -> bool:
        parts = Path(path).parts
        if not parts or Path(path).is_absolute() or ".." in parts or ".git" in parts or ".agilecode" in parts or ".agile_code" in parts:
            return False
        if any(p in SENSITIVE_NAMES or (p.startswith('.env.') and p not in {'.env.example', '.env.sample'}) for p in parts):
            return False
        return tracked or not any(p in EXCLUDED_DIRS for p in parts)

    async def snapshot_tree(self, cwd: Path) -> str:
        """Write a tree of raw eligible bytes using a private index. HEAD/index untouched."""
        staged = (await self.run("ls-files", "--stage", "-z", cwd=cwd)).stdout
        tracked = set()
        for entry in staged.split(b"\0"):
            if not entry:
                continue
            info, path = entry.split(b"\t", 1)
            mode = info.split()[0]
            if mode == b"160000":
                raise AgileCodeError("Submodule snapshots are not supported in this release.")
            name = path.decode("utf-8", errors="surrogateescape")
            if not self.eligible(name, tracked=True):
                raise AgileCodeError("Tracked protected/internal path cannot be silently omitted from a snapshot: " + name)
            tracked.add(name)
        paths = [p for p in await self.files(cwd) if self.eligible(p, p in tracked)]
        records: list[bytes] = []
        observed: list[tuple[Path, tuple[int, int, int, int]]] = []
        for rel in paths:
            file = cwd / rel
            if not inside(file.parent.resolve(), cwd.resolve()):
                raise AgileCodeError(f"Snapshot path has a parent symlink outside the worktree: {rel}")
            try:
                st = file.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                continue
            if stat.S_ISLNK(st.st_mode):
                data = os.fsencode(os.readlink(file))
                oid = (await self.run("hash-object", "-w", "--stdin", input=data, cwd=cwd)).text
                mode = "120000"
            elif stat.S_ISREG(st.st_mode):
                # Let Git hash from disk; large files never need to be read into Python.
                oid = (await self.run("hash-object", "-w", str(file), cwd=cwd)).text
                mode = "100755" if st.st_mode & 0o111 else "100644"
            else:
                raise AgileCodeError(f"Special files are not supported in snapshots: {rel}")
            records.append(f"{mode} {oid}\t".encode() + os.fsencode(rel) + b"\0")
            observed.append((file, (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode)))
        # Snapshotting is also a standalone adapter operation; it must not rely
        # on Application.open() having created the private runtime directory.
        index_dir = self.root / ".agilecode"
        index_dir.mkdir(exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="agilecode-index-", dir=index_dir)
        os.close(fd)
        os.unlink(temp_name)  # Git needs an absent index, not a zero-byte invalid index.
        env = {"GIT_INDEX_FILE": temp_name}
        try:
            await self.run("read-tree", "--empty", cwd=cwd, env=env)
            if records:
                await self.run("update-index", "-z", "--index-info", cwd=cwd, env=env, input=b"".join(records))
            tree = (await self.run("write-tree", cwd=cwd, env=env)).text
            for file, previous in observed:
                try:
                    st = file.lstat()
                except FileNotFoundError as exc:
                    raise Conflict("A file disappeared during snapshot creation.") from exc
                if previous != (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode):
                    raise Conflict("Files changed during snapshot creation; retry at a quiescent boundary.")
            current_paths = [p for p in await self.files(cwd) if self.eligible(p, p in tracked) and (cwd/p).exists()]
            if set(current_paths) != {str(p.relative_to(cwd)) for p, _ in observed if p.exists()}:
                raise Conflict("The file set changed while creating the snapshot.")
            return tree
        finally:
            for path in (temp_name, temp_name + ".lock"):
                if os.path.exists(path):
                    os.unlink(path)

    async def tree_entries(self, oid: str) -> dict[str, tuple[str, str]]:
        raw = (await self.run("ls-tree", "-r", "-z", oid)).stdout
        result = {}
        for entry in raw.split(b"\0"):
            if entry:
                info, path = entry.split(b"\t", 1)
                mode, kind, value = info.decode().split()
                if kind != "blob":
                    raise AgileCodeError("This tree includes unsupported submodule/special entries.")
                result[os.fsdecode(path)] = (mode, value)
        return result

    async def commit_object(self, tree: str, parents: list[str], message: str) -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        env = {"GIT_AUTHOR_NAME": "AgileCode Checkpoint", "GIT_AUTHOR_EMAIL": "checkpoint@agilecode.local",
               "GIT_COMMITTER_NAME": "AgileCode Checkpoint", "GIT_COMMITTER_EMAIL": "checkpoint@agilecode.local"}
        return (await self.run(*args, input=message.encode(), env=env)).text

    async def publish_ref(self, ref: str, oid: str, expected: str | None = None) -> None:
        if not ref.startswith("refs/agilecode/"):
            raise ValueError("Only private refs may use this method.")
        # Empty old value means the ref must not already exist; works for SHA-1/SHA-256.
        await self.run("update-ref", ref, oid, expected or "")

    async def diff(self, cwd: Path, base: str, *, tree: str | None = None) -> str:
        if tree is None:
            tree = await self.snapshot_tree(cwd)
        return (await self.run("diff", "--no-ext-diff", "--no-textconv", "--find-renames", base, tree, "--", cwd=cwd)).stdout.decode("utf-8", errors="replace")

    async def final_commit(self, cwd: Path, branch: str, base_oid: str, expected_tree: str, message: str, *, allow_empty=False) -> str:
        await self.verify_worktree(cwd, branch)
        if await self.head(cwd) != base_oid:
            raise Conflict("Ticket HEAD changed. The standard process permits no intermediate branch commits.")
        if await self.snapshot_tree(cwd) != expected_tree:
            raise Conflict("The worktree changed after verification/review.")
        base_tree = (await self.run("rev-parse", f"{base_oid}^{{tree}}", cwd=cwd)).text
        if base_tree == expected_tree and not allow_empty:
            raise AgileCodeError("There are no eligible source changes. No empty final commit was created.")
        # Do not stage protected/runtime paths even if somebody tracked them externally.
        await self.run("read-tree", expected_tree, cwd=cwd)
        args = ["commit", "--file", "-"]
        if allow_empty:
            args.append("--allow-empty")
        await self.run(*args, cwd=cwd, input=(message.strip() + "\n").encode())
        oid = await self.head(cwd)
        tree = (await self.run("rev-parse", "HEAD^{tree}", cwd=cwd)).text
        parents = (await self.run("rev-list", "--parents", "-n", "1", oid, cwd=cwd)).text.split()[1:]
        if tree != expected_tree or parents != [base_oid] or await self.snapshot_tree(cwd) != expected_tree:
            raise Conflict(f"Commit {oid} exists but a hook/external change altered the reviewed state. Reconcile manually; no reset was performed.")
        return oid

    async def remove_worktree(self, path: Path, expected_head: str, disposable: set[str] | None = None) -> None:
        await self.verify_worktree(path)
        if await self.head(path) != expected_head or await self.dirty(path):
            raise Conflict("Cleanup blocked: the worktree has new changes or a different HEAD.")
        ignored = (await self.run("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=path)).stdout
        disposable = disposable or (EXCLUDED_DIRS - {".git", ".agilecode"})
        unknown = [os.fsdecode(p) for p in ignored.split(b"\0") if p and not any(part in disposable for part in Path(os.fsdecode(p)).parts)]
        if unknown:
            raise Conflict(f"Cleanup preserved unknown ignored files: {', '.join(unknown[:8])}")
        async with self.admin_lock:
            # Force is permitted only after the above source/untracked/ignored checks.
            await self.run("worktree", "remove", "--force", str(path))

    async def delete_owned_worktree(self, path: Path, expected_branch: str | None = None) -> None:
        """Destructively remove an explicitly user-deleted AgileCode worktree.

        Unlike normal cleanup, dirty/untracked contents are intentionally discarded.
        The caller must first establish that path belongs to the ticket being
        deleted; this method additionally verifies Git worktree registration and
        optional branch identity before removal.
        """
        resolved = path.resolve()
        if resolved == self.root:
            raise Conflict("Refusing to delete the original project checkout.")
        trees = await self.worktrees()
        record = trees.get(str(resolved))
        if record is None:
            raise Conflict("The recorded ticket worktree is not registered with this repository.")
        if expected_branch is not None and record.get("branch") != f"refs/heads/{expected_branch}":
            raise Conflict("The recorded worktree no longer owns the ticket branch.")
        async with self.admin_lock:
            await self.run("worktree", "remove", "--force", str(resolved))

    async def delete_owned_branch(self, branch: str) -> None:
        """Delete a ticket-owned branch after all worktrees using it are gone."""
        ref = f"refs/heads/{branch}"
        users = [path for path, record in (await self.worktrees()).items() if record.get("branch") == ref]
        if users:
            raise Conflict("Ticket branch is still checked out in a worktree: " + ", ".join(users[:3]))
        actual = await self.run("rev-parse", "--verify", ref, check=False)
        if actual.code == 0:
            await self.run("branch", "-D", branch)
