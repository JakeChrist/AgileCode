"""Practical Bash containment checks, not a security sandbox or full shell parser.

Known direct escapes are denied. Shell constructs that cannot be fully understood
are not permission decisions; command permission is handled separately.
"""
from __future__ import annotations
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from agilecode.core.types import AccessDenied
from .workspace import Workspace, inside


@dataclass(frozen=True)
class CommandDecision:
    decision: str  # allow, deny
    reason: str
    cwd: str


class CommandGuard:
    READ_GIT = {"status", "diff", "log", "show", "ls-files", "rev-parse", "grep", "blame", "check-ignore", "ls-tree", "cat-file"}
    MUTATORS = {"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "truncate"}
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def _path(self, token: str, cwd: Path, *, write=True) -> Path:
        token = token.replace("${PWD}", str(cwd)).replace("$PWD", str(cwd))
        token = token.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
        if any(c in token for c in ("$", "`", "*", "?", "[", "{")):
            raise ValueError(f"Destination cannot be determined statically: {token}")
        raw = Path(token).expanduser()
        target = raw if raw.is_absolute() else cwd / raw
        return self.workspace.resolve(str(target), write=write)

    def validate(self, command: str, cwd: Path) -> CommandDecision:
        cwd = cwd.resolve()
        if not inside(cwd, self.workspace.root):
            return CommandDecision("deny", "Shell cwd is outside this ticket worktree; recreate the shell.", str(cwd))
        if not command.strip() or "\x00" in command or len(command) > 100_000:
            return CommandDecision("deny", "Empty, NUL-containing or oversized command.", str(cwd))
        if re.search(r"(?:^|[\s;])(?:export\s+)?GIT_[A-Za-z_0-9]*\s*=", command):
            return CommandDecision("deny", "Git administration/environment overrides are engine-owned.", str(cwd))
        try:
            lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
            lex.whitespace = " \t\r"
            lex.commenters = "#"
            tokens = list(lex)
        except ValueError as exc:
            return CommandDecision("deny", f"Shell command cannot be parsed safely: {exc}", str(cwd))
        pieces: list[list[str]] = []
        piece: list[str] = []
        for token in tokens:
            if token in {";", "&&", "||", "|", "\n"} or token.isspace():
                if piece: pieces.append(piece); piece = []
            elif token == "&":
                return CommandDecision("deny", "Use background=true for a managed job instead of raw '&'.", str(cwd))
            elif token in {"(", ")", "((", "))"}:
                indeterminate = True
            else:
                piece.append(token)
        if piece: pieces.append(piece)
        observed_cwd = cwd
        reason = "Recognized direct paths remain in the worktree. Programs may have indirect effects."
        try:
            for raw in pieces:
                # Inspect redirections before removing them from the argv-like view.
                words: list[str] = []
                i = 0
                while i < len(raw):
                    token = raw[i]
                    if token in {">", ">>", ">|", "&>", "&>>", "<", "<<", "<<<", "<>"}:
                        if i + 1 >= len(raw):
                            raise ValueError("Incomplete redirection")
                        destination = raw[i+1]
                        if token.startswith("<<"):
                                elif destination != "/dev/null":
                            self._path(destination, observed_cwd, write=token != "<")
                        if words and words[-1].isdigit():
                            words.pop()
                        i += 2
                        continue
                    if token in {">&", "<&"}:
                        # fd duplication is not a filesystem path, but using arbitrary
                        # descriptors could interfere with the shell control channel.
                        if i+1 >= len(raw) or raw[i+1] not in {"0", "1", "2", "-"}:
                            raise ValueError("Nonstandard descriptor redirection")
                        i += 2
                        continue
                    words.append(token); i += 1
                while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
                    assignment = words.pop(0)
                    if assignment.split("=",1)[0] in {"HOME", "BASH_ENV", "ENV", "LD_PRELOAD"}:
                        raise AccessDenied("Changing execution-control variables requires explicit environment configuration.")
                if not words:
                    continue
                executable = Path(words[0]).name
                args = words[1:]
                if executable in {"command", "builtin", "env"}:
                    # Inspect wrapped Git commands as well; do not let 'env git commit' through.
                    if "git" in args:
                        words = args[args.index("git"):]; executable = "git"; args = words[1:]
                if executable in {"cd", "pushd"}:
                    targets = [a for a in args if a not in {"--", "-L", "-P"}]
                    if len(targets) != 1 or targets[0] in {"-", ""}:
                        raise ValueError("Use an explicit in-worktree directory")
                    observed_cwd = self._path(targets[0], observed_cwd, write=False)
                    if not observed_cwd.is_dir():
                        raise ValueError(f"Directory does not exist: {targets[0]}")
                elif executable == "popd":
                    raise ValueError("Directory-stack destination cannot be inferred; use explicit cd")
                elif executable == "git":
                    j = 0
                    while j < len(args) and args[j].startswith("-"):
                        arg = args[j]
                        if arg == "-C" and j+1 < len(args):
                            target = self._path(args[j+1], observed_cwd, write=False)
                            if not inside(target, self.workspace.root):
                                raise AccessDenied("Git targets another checkout.")
                            j += 2
                        elif arg in {"--no-pager", "--paginate"}:
                            j += 1
                        else:
                            raise AccessDenied("Git global overrides are not allowed; use the provided Git tools.")
                    if j >= len(args) or args[j] not in self.READ_GIT:
                        raise AccessDenied("Git commits, refs, branches, configuration and integration are managed by the process engine.")
                    for arg in args[j+1:]:
                        if arg.startswith("--output="):
                            self._path(arg.split("=",1)[1], observed_cwd)
                        if arg in {"--output", "--ext-diff", "--textconv", "--no-index"}:
                            raise AccessDenied("This Git option requires an engine-owned operation.")
                elif executable in {"cp", "mv", "install"}:
                    destination = None
                    positional = []
                    j = 0
                    while j < len(args):
                        arg = args[j]
                        if arg in {"-t", "--target-directory"}:
                            if j+1 >= len(args): raise ValueError("Missing destination")
                            destination = args[j+1]; j += 2; continue
                        if arg.startswith("--target-directory="):
                            destination = arg.split("=",1)[1]
                        elif not arg.startswith("-"):
                            positional.append(arg)
                        j += 1
                    destination = destination or (positional[-1] if positional else None)
                    if not destination:
                        raise ValueError("Cannot determine copy/move destination")
                    self._path(destination, observed_cwd)
                    if executable == "mv":
                        for source in positional[:-1]:
                            self._path(source, observed_cwd)
                elif executable in self.MUTATORS or executable == "tee":
                    positional = [a for a in args if not a.startswith("-")]
                    if executable in {"chmod", "chown"}:
                        positional = positional[1:]
                    for target in positional:
                        if target != "/dev/null":
                            self._path(target, observed_cwd)
                elif executable in {"sed", "perl"} and any(a == "-i" or a.startswith("-i") for a in args):
                    raise ValueError("In-place stream-editor writes require review; prefer edit_file")
                elif executable in {"python", "python3"} and "-c" in args:
                elif executable in {"source", "."}:
                    if not args:
                        raise ValueError("Missing sourced file")
                    self._path(args[0], observed_cwd, write=False)
                    # Sourced files can change cwd; it is observed again after execution.
                elif executable == "export":
                    if any(a.split("=",1)[0] in {"HOME", "BASH_ENV", "ENV", "LD_PRELOAD"} for a in args):
                        raise AccessDenied("Execution-control environment changes require configuration.")
            return CommandDecision("allow", reason, str(cwd))
        except AccessDenied as exc:
            return CommandDecision("deny", str(exc), str(cwd))
        except (ValueError, OSError) as exc:
            return CommandDecision("deny", str(exc), str(cwd))
