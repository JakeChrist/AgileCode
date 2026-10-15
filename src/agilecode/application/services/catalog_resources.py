"""Canonical skill and reusable-profile catalog policies.

Legacy adapter/core import paths re-export these classes without separate policy
bodies. Filesystem parsing and live-content behavior intentionally stay unchanged.
"""
from __future__ import annotations
from copy import deepcopy
import json
import re
from pathlib import Path

import yaml
from pydantic import Field

from agilecode.core.types import AccessDenied, AgileCodeError, Conflict, DataModel, uid
from agilecode.adapters.config_lock import atomic_json, atomic_bytes, shared_file_lock
from agilecode.adapters.workspace import digest, inside

_UNSET = object()


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


class PMProfile(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    instructions: str = Field(default="")
    skills: list[str] = Field(default_factory=list)


class PMProfileLibrary:
    def __init__(self, global_root: Path):
        self.root = global_root / "project_manager" / "profiles"
        self._snapshots = {}
        self.coordinator = None

    def install_bundled(self, *, owner=None) -> None:
        with shared_file_lock(self.root / ".catalog", owner=owner):
            self._install_bundled_locked()

    def _install_bundled_locked(self) -> None:
        """Seed packaged PM profiles without replacing profiles edited by the user."""
        source = Path(__file__).resolve().parents[2] / "resources" / "pm_profiles"
        if not source.is_dir():
            return
        for bundled in sorted(source.glob("*.json")):
            profile = PMProfile.model_validate_json(bundled.read_text(encoding="utf-8"))
            if profile.id != bundled.stem:
                raise AgileCodeError(f"Bundled PM profile ID does not match its filename: {bundled.name}")
            target = self.root / bundled.name
            if target.is_symlink():
                raise AgileCodeError(f"Bundled PM profile destination must not be a symlink: {target}")
            if target.exists():
                if not target.is_file():
                    raise AgileCodeError(f"Bundled PM profile destination is not a file: {target}")
                self._install_review_skill_upgrade(profile.id, target)
                continue  # Preserve locally customized profile instructions and skills.
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_bytes(target, bundled.read_bytes())

    def _install_review_skill_upgrade(self, identity: str, target: Path) -> None:
        """One-time addition for users with the original unmodified built-in skill selection."""
        if identity != "lead-systems-engineer":
            return
        marker = self.root / ".lead-systems-engineer-reviews-v1"
        if marker.is_symlink():
            raise AgileCodeError(f"Bundled profile upgrade marker must not be a symlink: {marker}")
        if marker.exists():
            return

        current = self.get(identity)
        previous_skills = [
            "systems-engineering-lifecycle",
            "technical-planning-delegation",
            "system-integration-oversight",
            "system-level-assessment",
        ]
        if current.skills == previous_skills:
            # Preserve edited instructions/name; do not modify customized skill sets.
            updated = current.model_copy(update={
                "skills": [*current.skills, "systems-engineering-technical-reviews"]
            })
            atomic_json(target, updated.model_dump(mode="json"))
        # Record that this one-time upgrade was considered. User changes made
        # afterward, including removing the new skill, must not be undone.
        atomic_bytes(marker, b"")

    def get(self, identity: str) -> PMProfile:
        if not identity or not identity.replace("-", "").replace("_", "").isalnum():
            raise AgileCodeError("Invalid Project Manager profile ID.")
        path = self.root / f"{identity}.json"
        if not path.is_file() or path.is_symlink():
            raise AgileCodeError(f"Project Manager profile {identity!r} was not found.")
        try:
            profile = PMProfile.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AgileCodeError(f"Cannot read Project Manager profile {identity!r}: {exc}") from exc
        if profile.id != identity:
            raise AgileCodeError("Project Manager profile ID does not match its filename.")
        self._snapshots[identity] = profile.model_dump(mode="json")
        return profile

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        profiles = []
        for path in sorted(self.root.glob("*.json")):
            if not path.is_symlink():
                profiles.append(self.get(path.stem).model_dump(mode="json"))
        return sorted(profiles, key=lambda item: (item["name"].casefold(), item["id"]))

    def save(self, data: dict, *, expected=_UNSET) -> dict:
        self._check_admission()
        payload = dict(data)
        identity = payload.get("id") or uid()
        payload["id"] = identity
        profile = PMProfile.model_validate(payload)
        path = self.root / f"{profile.id}.json"
        with shared_file_lock(self.root / ".catalog"):
            if path.is_symlink():
                raise AgileCodeError("Cannot overwrite a linked profile file.")
            prior = self._snapshots.get(identity, _UNSET) if expected is _UNSET else expected
            if data.get("id") is not None:
                # Compare the captured draft before get() refreshes the cache.
                current = self.get(profile.id).model_dump(mode="json")
                if prior is not _UNSET and current != prior:
                    self._snapshots[identity] = prior
                    raise Conflict("Project Manager profile changed since it was read. Reload before saving.")
            elif path.exists():
                raise Conflict("Project Manager profile identity already exists.")
            elif prior is not _UNSET and prior is not None:
                raise Conflict("New Project Manager profile expected value must be absent.")
            atomic_json(path, profile.model_dump(mode="json"))
        saved = profile.model_dump(mode="json")
        self._snapshots[identity] = deepcopy(saved)
        return saved

    def delete(self, identity: str, *, expected=_UNSET) -> None:
        self._check_admission()
        with shared_file_lock(self.root / ".catalog"):
            prior = self._snapshots.get(identity, _UNSET) if expected is _UNSET else expected
            current = self.get(identity).model_dump(mode="json")
            if prior is not _UNSET and current != prior:
                self._snapshots[identity] = prior
                raise Conflict("Project Manager profile changed since it was read. Reload before deleting.")
            (self.root / f"{identity}.json").unlink()
            self._snapshots.pop(identity, None)

    def _check_admission(self):
        if self.coordinator is not None and self.coordinator.maintenance_owner:
            raise Conflict("Project maintenance is in progress. Retry after it finishes.")
