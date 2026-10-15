"""One local Git command operation with explicit project/worktree execution."""
from __future__ import annotations
import shlex
from agilecode.core.types import AccessDenied, AgileCodeError

# Only git_command uses this policy. run_command and engine-owned Git operations
# retain their existing behavior.
_REMOTE_GIT_OPERATIONS = frozenset({
    "clone", "fetch", "pull", "push", "ls-remote", "remote",
    "send-email", "imap-send", "svn", "p4", "daemon",
    "upload-pack", "receive-pack", "upload-archive",
    "http-backend", "http-fetch", "http-push",
})
_GIT_GLOBAL_OPTIONS_WITH_VALUES = frozenset({
    "-C", "-c", "--config-env", "--git-dir", "--work-tree",
    "--namespace", "--exec-path", "--attr-source", "--super-prefix",
})


def validate_git_command(command: str) -> list[str]:
    """Apply a default-allow, remote-only blacklist to every agent git_command."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise AgileCodeError(f"Invalid Git command: {exc}") from exc
    if not argv or argv[0] != "git":
        raise AgileCodeError("git_command only accepts commands whose executable is git.")

    # Locate the Git verb after global flags (for example git -C path fetch).
    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        if option == "--":
            index += 1
            break
        if option in _GIT_GLOBAL_OPTIONS_WITH_VALUES:
            if index + 1 >= len(argv):
                raise AgileCodeError(f"Missing value for Git option {option}.")
            if option in {"-c", "--config-env"} and argv[index + 1].lower().startswith("alias."):
                raise AccessDenied("git_command cannot override Git aliases.")
            index += 2
        else:
            if option.startswith("-c") and option[2:].lower().startswith("alias."):
                raise AccessDenied("git_command cannot override Git aliases.")
            if option.lower().startswith("--config-env=alias."):
                raise AccessDenied("git_command cannot override Git aliases.")
            index += 1

    operation = argv[index] if index < len(argv) else ""
    arguments = argv[index + 1:]
    if operation in _REMOTE_GIT_OPERATIONS:
        raise AccessDenied(f"Remote Git operation '{operation}' is disabled in git_command.")
    if operation == "submodule" and any(arg in {"add", "update", "foreach"} for arg in arguments):
        raise AccessDenied("Remote-capable Git submodule operations are disabled in git_command.")
    if operation == "lfs" and any(arg in {"clone", "fetch", "pull", "push", "lock", "unlock", "locks"} for arg in arguments):
        raise AccessDenied("Remote Git LFS operations are disabled in git_command.")
    if operation == "archive" and any(arg == "--remote" or arg.startswith("--remote=") for arg in arguments):
        raise AccessDenied("Remote Git archives are disabled in git_command.")
    if operation == "maintenance" and any(arg == "start" or "prefetch" in arg for arg in arguments):
        raise AccessDenied("Remote Git maintenance is disabled in git_command.")

    # Cached remote-tracking history must not be used as an alternative to
    # contacting a remote. Stop scanning at -- so ordinary file paths remain usable.
    for arg in arguments:
        if arg == "--":
            break
        ref = arg.lower()
        if (
            "refs/remotes/" in ref
            or "remotes/" in ref
            or "@{upstream}" in ref
            or "@{u}" in ref
            or "origin/" in ref
            or "upstream/" in ref
            or ref == "--remotes"
            or ref.startswith("--remotes=")
        ):
            raise AccessDenied("Remote-tracking references are disabled in git_command.")
        if operation in {"log", "rev-list", "reflog", "rev-parse"} and ref == "--all":
            raise AccessDenied("Enumerating remote-tracking history is disabled in git_command.")
        if operation == "branch" and (
            ref in {"-r", "-a", "--remotes", "--all"}
            or (ref.startswith("-") and not ref.startswith("--") and any(c in ref[1:] for c in "ra"))
        ):
            raise AccessDenied("Listing remote-tracking branches is disabled in git_command.")
    if operation == "for-each-ref" and not any(
        arg.startswith("refs/heads/") or arg == "refs/heads" or arg.startswith("refs/tags/")
        for arg in arguments
    ):
        raise AccessDenied("Unscoped Git ref listings may include remote-tracking references.")
    return argv


async def run_git_command(command, *, git=None, shell=None, attempt_id=None, settings=None, write_lock=None):
    argv = validate_git_command(command)
    if shell is not None:
        if git is not None or settings is None or write_lock is None:
            raise AgileCodeError("Ticket Git requires its shell, settings and write reservation.")
        async with write_lock:
            return await shell.run(shlex.join(argv), attempt_id,
                timeout=settings().shell_command_timeout_seconds, background=False)
    if git is None:
        raise AgileCodeError("A Git execution scope is required.")
    result = await git.run(*argv[1:], check=False)
    return {"status": "completed", "exit_code": result.code,
            "stdout": result.stdout.decode(errors="replace"),
            "stderr": result.stderr.decode(errors="replace")}
