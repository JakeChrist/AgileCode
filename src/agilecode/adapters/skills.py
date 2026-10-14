"""Global/project skill discovery with explicit errors and on-demand instructions."""
from __future__ import annotations
import re
from pathlib import Path
import yaml
from agilecode.core.types import AccessDenied, AgileCodeError
from .workspace import digest, inside


class SkillRegistry:
    def __init__(self, global_root: Path, project_root: Path):
        self.roots = [("global", global_root / "skills"), ("project", project_root / ".agilecode" / "skills")]
    def discover(self) -> list[dict]:
        result = []
        for scope, root in self.roots:
            if not root.exists():
                continue
            for folder in sorted(root.iterdir()):
                if not folder.is_dir():
                    continue
                record = {"scope": scope, "path": str(folder / "SKILL.md"), "id": folder.name}
                try:
                    file = folder / "SKILL.md"
                    if not inside(file.resolve(), root.resolve()):
                        raise AccessDenied("Skill link points outside its configured skill directory.")
                    data = file.read_bytes()
                    text = data.decode("utf-8-sig")
                    if not text.startswith("---\n") and not text.startswith("---\r\n"):
                        raise AgileCodeError("SKILL.md requires YAML frontmatter.")
                    pieces = text.split("---", 2)
                    if len(pieces) != 3:
                        raise AgileCodeError("Unclosed skill frontmatter.")
                    metadata = yaml.safe_load(pieces[1])
                    if not isinstance(metadata, dict) or metadata.get("name") != folder.name:
                        raise AgileCodeError("Skill name must match its directory.")
                    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", folder.name):
                        raise AgileCodeError("Use lowercase letters, numbers and hyphens in skill IDs.")
                    description = metadata.get("description", "")
                    if not isinstance(description, str) or not description.strip() :
                        raise AgileCodeError("Description must contain 1–1024 characters.")
                    record.update(description=description, hash=digest(data), instructions=pieces[2].strip())
                except Exception as exc:
                    record["error"] = str(exc)
                result.append(record)
        # Keep invalid records visible; label valid overrides rather than dropping them.
        project_ids = {r["id"] for r in result if r["scope"] == "project" and "error" not in r}
        for record in result:
            record["overridden"] = record["scope"] == "global" and record["id"] in project_ids
        return result
    def catalog(self, allowed: list[str] | None = None) -> list[dict]:
        records = self.discover()
        return [{k: v for k, v in record.items() if k != "instructions"} for record in records
                if allowed is None or "*" in allowed or record["id"] in allowed]
    def load(self, identity: str, allowed: list[str]) -> dict:
        if "*" not in allowed and identity not in allowed:
            raise AccessDenied("This skill is not enabled for the assigned team member.")
        for record in reversed(self.discover()):
            if record["id"] == identity and "error" not in record and not record["overridden"]:
                return record
        raise AgileCodeError(f"Skill {identity!r} is unavailable. Inspect skill discovery errors.")
