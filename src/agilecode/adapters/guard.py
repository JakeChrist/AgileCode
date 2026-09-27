"""Practical Bash containment checks, not a security sandbox or full shell parser.

Known direct escapes are denied. Shell constructs that cannot be fully understood
are not permission decisions; command permission is handled separately.
"""
from __future__ import annotations
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from agilecode.core.types import AccessDenied
from .workspace import Workspace, inside


@dataclass(frozen=True)
class ContainmentDecision:
    decision: Literal["ok", "block"]
    reason: str
    cwd: str


class CommandGuard:
    READ_GIT = {"status", "diff", "log", "show", "ls-files", "rev-parse", "grep", "blame", "check-ignore", "ls-tree", "cat-file"}
    MUTATORS = {"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "truncate"}
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def _path(self, token: str, cwd: Path, *, write=True) -> Path | None:
        token = token.replace("${PWD}", str(cwd)).replace("$PWD", str(cwd))
        token = token.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
        uncertain = [token.find(c) for c in ("$", "`", "*", "?", "[", "{") if c in token]
        if uncertain:
            # A wildcard/expansion can make the final destination unknowable, but
            # an explicit static prefix that already escapes the worktree is still
            # a containment violation (for example ../* or /tmp/*).
            prefix = token[:min(uncertain)]
            if prefix and ("/" in prefix or prefix.startswith(".")):
                raw_prefix = Path(prefix).expanduser()
                target_prefix = raw_prefix if raw_prefix.is_absolute() else cwd / raw_prefix
                self.workspace.resolve(str(target_prefix), write=write)
            return None  # Dynamic suffix; continue checking other explicit paths.
        raw = Path(token).expanduser()
        target = raw if raw.is_absolute() else cwd / raw
        return self.workspace.resolve(str(target), write=write)

    def validate(self, command: str, cwd: Path) -> ContainmentDecision:
        cwd = cwd.resolve()
        if not inside(cwd, self.workspace.root):
            return ContainmentDecision("block", "Shell cwd is outside this ticket worktree; recreate the shell.", str(cwd))
        if not command.strip() or "\x00" in command or len(command) > 100_000:
            return ContainmentDecision("block", "Empty, NUL-containing or oversized command.", str(cwd))
        if re.search(r"(?:^|[\s;])(?:export\s+)?GIT_[A-Za-z_0-9]*\s*=", command):
            return ContainmentDecision("block", "Git administration/environment overrides are engine-owned.", str(cwd))

        try:
            lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
            lex.whitespace = " \t\r"
            lex.commenters = "#"
            tokens = list(lex)
        except ValueError as exc:
            return ContainmentDecision(
                "ok",
                f"Containment could not fully parse this command ({exc}); no explicit boundary violation was identified.",
                str(cwd),
            )

        pieces: list[list[str]] = []
        piece: list[str] = []
        for token in tokens:
            if token in {";", "&&", "||", "|", "\n", "(", ")", "((", "))"} or token.isspace():
                if piece:
                    pieces.append(piece)
                    piece = []
            elif token == "&":
                return ContainmentDecision("block", "Use background=true for a managed job instead of raw '&'.", str(cwd))
            else:
                piece.append(token)
        if piece:
            pieces.append(piece)

        observed_cwd = cwd
        reason = "Recognized direct paths remain in the worktree. Programs may have indirect effects."
        try:
            for raw in pieces:
                try:
                    words: list[str] = []
                    i = 0
                    while i < len(raw):
                        token = raw[i]
                        if token in {">", ">>", ">|", "&>", "&>>", "<", "<<", "<<<", "<>"}:
                            if i + 1 >= len(raw):
                                raise ValueError("Incomplete redirection")
                            destination = raw[i + 1]
                            if not token.startswith("<<") and destination != "/dev/null":
                                self._path(destination, observed_cwd, write=token != "<")
                            if words and words[-1].isdigit():
                                words.pop()
                            i += 2
                            continue
                        if token in {">&", "<&"}:
                            if i + 1 >= len(raw) or raw[i + 1] not in {"0", "1", "2", "-"}:
                                raise ValueError("Nonstandard descriptor redirection")
                            i += 2
                            continue
                        words.append(token)
                        i += 1

                    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
                        assignment = words.pop(0)
                        if assignment.split("=", 1)[0] in {"HOME", "BASH_ENV", "ENV", "LD_PRELOAD"}:
                            raise AccessDenied("Changing execution-control variables requires explicit environment configuration.")
                    if not words:
                        continue

                    executable = Path(words[0]).name
                    args = words[1:]

                    if executable == "sudo":
                        # Permission to use sudo is a CommandPolicy decision. Once
                        # permitted, still inspect an obvious wrapped command for the
                        # same hard worktree/Git boundaries.
                        value_options = {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--chdir"}
                        j = 0
                        while j < len(args) and args[j].startswith("-"):
                            if args[j] == "--":
                                j += 1
                                break
                            option = args[j].split("=", 1)[0]
                            j += 2 if option in value_options and "=" not in args[j] else 1
                        if j < len(args):
                            words = args[j:]
                            executable = Path(words[0]).name
                            args = words[1:]

                    if executable in {"command", "builtin", "env"}:
                        if executable == "env":
                            while args and re.match(r"^[A-Za-z_]\w*=", args[0]):
                                args = args[1:]
                        if args:
                            words = args
                            executable = Path(words[0]).name
                            args = words[1:]

                    if executable in {"bash", "sh", "zsh"} and "-c" in args:
                        index = args.index("-c")
                        if index + 1 < len(args):
                            nested = self.validate(args[index + 1], observed_cwd)
                            if nested.decision == "block":
                                raise AccessDenied(nested.reason)

                    if executable in {"cd", "pushd"}:
                        targets = [a for a in args if a not in {"--", "-L", "-P"}]
                        if len(targets) != 1 or targets[0] in {"-", ""}:
                            raise ValueError("Use an explicit in-worktree directory")
                        target = self._path(targets[0], observed_cwd, write=False)
                        if target is None:
                            continue
                        observed_cwd = target
                        if not observed_cwd.is_dir():
                            raise ValueError(f"Directory does not exist: {targets[0]}")
                    elif executable == "popd":
                        raise ValueError("Directory-stack destination cannot be inferred; use explicit cd")
                    elif executable == "git":
                        j = 0
                        while j < len(args) and args[j].startswith("-"):
                            arg = args[j]
                            if arg == "-C" and j + 1 < len(args):
                                target = self._path(args[j + 1], observed_cwd, write=False)
                                if target is not None and not inside(target, self.workspace.root):
                                    raise AccessDenied("Git targets another checkout.")
                                j += 2
                            elif arg in {"--no-pager", "--paginate"}:
                                j += 1
                            else:
                                raise AccessDenied("Git global overrides are not allowed; use the provided Git tools.")
                        if j >= len(args) or args[j] not in self.READ_GIT:
                            raise AccessDenied("Git commits, refs, branches, configuration and integration are managed by the process engine.")
                        for arg in args[j + 1:]:
                            if arg.startswith("--output="):
                                self._path(arg.split("=", 1)[1], observed_cwd)
                            if arg in {"--output", "--ext-diff", "--textconv", "--no-index"}:
                                raise AccessDenied("This Git option requires an engine-owned operation.")
                    elif executable in {"cp", "mv", "install"}:
                        destination = None
                        positional = []
                        j = 0
                        while j < len(args):
                            arg = args[j]
                            if arg in {"-t", "--target-directory"}:
                                if j + 1 >= len(args):
                                    raise ValueError("Missing destination")
                                destination = args[j + 1]
                                j += 2
                                continue
                            if arg.startswith("--target-directory="):
                                destination = arg.split("=", 1)[1]
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
                        # These are direct file writes. Validate path-looking operands,
                        # but do not turn parser uncertainty into a permission prompt.
                        for target in [a for a in args if not a.startswith("-")][1:]:
                            if "/" in target or target.startswith("."):
                                self._path(target, observed_cwd)
                    elif executable in {"source", "."}:
                        if not args:
                            raise ValueError("Missing sourced file")
                        self._path(args[0], observed_cwd, write=False)
                    elif executable == "export":
                        if any(a.split("=", 1)[0] in {"HOME", "BASH_ENV", "ENV", "LD_PRELOAD"} for a in args):
                            raise AccessDenied("Execution-control environment changes require configuration.")
                except (ValueError, OSError) as exc:
                    reason = f"Some direct effects could not be determined ({exc})."

            return ContainmentDecision("ok", reason, str(cwd))
        except AccessDenied as exc:
            return ContainmentDecision("block", str(exc), str(cwd))
        except (ValueError, OSError) as exc:
            return ContainmentDecision(
                "ok",
                f"Containment could not fully determine direct effects ({exc}); no explicit boundary violation was identified.",
                str(cwd),
            )
