"""Reusable global Project Manager roles, independent of autonomy and ticket permissions."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from agilecode.core.types import AgileCodeError, DataModel, uid


class PMProfile(DataModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=160)
    instructions: str = Field(default="", max_length=30_000)
    skills: list[str] = Field(default_factory=list)


class PMProfileLibrary:
    def __init__(self, global_root: Path):
        self.root = global_root / "project_manager" / "profiles"

    def install_bundled(self) -> None:
        """Seed packaged PM profiles without replacing profiles edited by the user."""
        source = Path(__file__).resolve().parents[1] / "resources" / "pm_profiles"
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
            try:
                with target.open("x", encoding="utf-8") as handle:
                    handle.write(bundled.read_text(encoding="utf-8"))
            except FileExistsError:
                pass

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
            temporary = target.with_suffix(".review-skill.tmp")
            try:
                temporary.write_text(updated.model_dump_json(indent=2) + "\n", encoding="utf-8")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        # Record that this one-time upgrade was considered. User changes made
        # afterward, including removing the new skill, must not be undone.
        marker.touch(exist_ok=True)

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
        return profile

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        profiles = []
        for path in sorted(self.root.glob("*.json")):
            if not path.is_symlink():
                profiles.append(self.get(path.stem).model_dump(mode="json"))
        return sorted(profiles, key=lambda item: (item["name"].casefold(), item["id"]))

    def save(self, data: dict) -> dict:
        payload = dict(data)
        identity = payload.get("id") or uid()
        payload["id"] = identity
        profile = PMProfile.model_validate(payload)
        path = self.root / f"{profile.id}.json"
        if path.is_symlink():
            raise AgileCodeError("Cannot overwrite a linked profile file.")
        if data.get("id") is not None:
            self.get(profile.id)  # Editing must not silently create a different profile.
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return profile.model_dump(mode="json")

    def delete(self, identity: str) -> None:
        self.get(identity)
        (self.root / f"{identity}.json").unlink()
