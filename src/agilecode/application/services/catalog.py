"""Catalog operations and the established packaged-resource update policies."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
from copy import deepcopy

from agilecode.core.types import Team
from agilecode.core.defaults import requirements_engineering_team, standard_team, systems_architecture_team
from agilecode.adapters.store import decode, encode
from agilecode.adapters.config_lock import atomic_json, atomic_bytes
from .configuration import _check, _read, _UNSET, configuration_write

from agilecode.adapters.config_lock import shared_file_lock
from agilecode.core.types import AgileCodeError, uid


class CatalogService:
    def __init__(self, configuration, skills, profiles):
        self.configuration, self.skills, self.profiles = configuration, skills, profiles
        configuration.catalog = self

    @property
    def coordinator(self):
        return self.configuration.coordinator

    async def list_teams(self) -> list[dict]:
        result = [
            {"ref": "builtin:standard", "scope": "builtin", "team": standard_team().model_dump(mode="json")},
            {
                "ref": "builtin:requirements-engineering",
                "scope": "builtin",
                "team": requirements_engineering_team().model_dump(mode="json"),
            },
            {
                "ref": "builtin:systems-architecture",
                "scope": "builtin",
                "team": systems_architecture_team().model_dump(mode="json"),
            },
        ]
        for path in sorted((self.configuration.root / "teams").glob("*.json")):
            try:
                team = Team.model_validate_json(path.read_text())
                result.append({"ref": f"global:{team.id}", "scope": "global", "team": team.model_dump(mode="json")})
            except Exception as exc:
                result.append({"ref": f"invalid:{path.name}", "scope": "global", "error": str(exc)})
        for row in await self.configuration.store.query("SELECT * FROM teams ORDER BY id"):
            team = Team.model_validate_json(row["data"])
            result.append({"ref": f"project:{team.id}", "scope": "project", "team": team.model_dump(mode="json")})
        self.configuration._team_base.update({item["ref"]: deepcopy(item["team"]) for item in result if "team" in item})
        return result

    async def get_team(self, reference: str) -> Team:
        for item in await self.list_teams():
            if item["ref"] == reference and "team" in item:
                return Team.model_validate(item["team"])
        raise AgileCodeError(f"Team {reference!r} is not available. Choose a team in this project or globally.")

    @configuration_write
    async def save_team(self, team: Team, scope: str, *, expected=_UNSET) -> str:
        ref = f"{scope}:{team.id}"
        baseline = self.configuration._team_base.get(ref) if expected is _UNSET else expected
        value = team.model_dump(mode="json")
        if scope == "global":
            path = self.configuration.root / "teams" / f"{team.id}.json"
            with shared_file_lock(self.configuration.root / "teams" / ".catalog"):
                raw = _read(path, None)
                current = Team.model_validate(raw).model_dump(mode="json") if raw is not None else None
                _check(current, baseline, f"Team {ref!r}")
                atomic_json(path, value)
        elif scope == "project":
            def write(conn):
                row = conn.execute("SELECT data FROM teams WHERE id=?", (team.id,)).fetchone()
                current = Team.model_validate_json(row[0]).model_dump(mode="json") if row else None
                _check(current, baseline, f"Team {ref!r}")
                conn.execute("INSERT INTO teams(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,revision=revision+1", (team.id, encode(value)))
            await self.configuration.store.transaction(write)
        else:
            raise AgileCodeError("Built-in teams are immutable. Save a global or project copy.")
        self.configuration._team_base[ref] = deepcopy(value)
        return ref

    async def tool_catalog(self, *, discover_external=True, mcp_factory=None):
        """Native metadata is cheap; external discovery is an explicit operation."""
        from agilecode.core.types import NATIVE_TOOL_NAMES
        tools = [{"name": name, "label": name} for name in NATIVE_TOOL_NAMES]
        if not discover_external:
            return {"tools": tools, "errors": {}}
        if mcp_factory is None:
            from agilecode.adapters.mcp import McpService
            mcp_factory = McpService
        project_root = self.configuration.store.path.parent.parent
        mcp = mcp_factory(self.configuration.settings.mcp_servers, project_root)
        try:
            await mcp.discover()
            tools.extend({"name": alias, "label": f"{remote['server']}: {remote['remote_name']}"}
                         for alias, remote in mcp.tools.items())
            return {"tools": tools, "errors": mcp.errors}
        finally:
            await mcp.close()

    def discover_skills(self):
        return self.skills.discover()

    def list_skills(self, allowed=None):
        return self.skills.catalog(allowed)

    def load_skill(self, identity, allowed):
        # The adapter reads the actual body on every call; its hash and source
        # describe exactly what this invocation loaded.
        return self.skills.load(identity, allowed)

    def list_profiles(self):
        return self.profiles.list()

    def get_profile(self, identity):
        return self.profiles.get(identity)

    def save_profile(self, data, **kwargs):
        from .catalog_resources import PMProfile
        profile = PMProfile.model_validate({**data, "id": data.get("id") or "new"})
        available = {item["id"] for item in self.skills.discover() if item["scope"] == "global" and "error" not in item}
        unavailable = set(profile.skills) - available
        if unavailable:
            raise AgileCodeError("Unknown global PM skills: " + ", ".join(sorted(unavailable)))
        return self.profiles.save(data, **kwargs)

    def delete_profile(self, identity, **kwargs):
        return self.profiles.delete(identity, **kwargs)

    async def install_bundled(self, *, owner=None):
        await self.install_skills(owner=owner)
        from agilecode.application.coordination import settle
        await settle(asyncio.to_thread(self.profiles.install_bundled, owner=owner))

    async def install_skills(self, *, owner=None):
        from agilecode.application.coordination import settle
        await settle(asyncio.to_thread(install_builtin_skills, self.configuration.root, owner=owner))


def install_builtin_skills(global_root: Path, source: Path | None = None, *, owner=None):
    """Back up differing replacements, then overlay, preserving extra user files."""
    source = source or Path(__file__).resolve().parents[2] / "resources" / "skills"
    if not source.exists():
        return
    with shared_file_lock(global_root / "skills" / ".catalog", owner=owner):
        for folder in sorted(source.iterdir()):
            if not folder.is_dir() or not (folder / "SKILL.md").is_file():
                continue
            target = global_root / "skills" / folder.name
            if target.is_symlink():
                raise AgileCodeError(f"Bundled skill directory must not be a symlink: {target}")
            if not target.exists():
                _overlay_skill(folder, target)
                continue
            if not target.is_dir():
                raise AgileCodeError(f"Bundled skill path is not a directory: {target}")
            incoming = [p for p in folder.rglob("*") if p.is_file()]
            changed = replaced = False
            for file in incoming:
                destination = target / file.relative_to(folder)
                if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents
                                                  if parent == target or target in parent.parents):
                    raise AgileCodeError(f"Bundled skill destination must not be a symlink: {destination}")
                if not destination.exists():
                    changed = True
                elif not destination.is_file():
                    raise AgileCodeError(f"Bundled skill destination is not a file: {destination}")
                elif destination.read_bytes() != file.read_bytes():
                    changed = replaced = True
            if not changed:
                continue
            if replaced:
                backup = global_root / "skill-backups" / f"{folder.name}-{uid()}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(target, backup)
            _overlay_skill(folder, target)


def _overlay_skill(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        destination = target / item.relative_to(source)
        if item.is_symlink():
            raise AgileCodeError(f"Bundled skill source must not be a symlink: {item}")
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            atomic_bytes(destination, item.read_bytes(), mode=item.stat().st_mode & 0o777)
