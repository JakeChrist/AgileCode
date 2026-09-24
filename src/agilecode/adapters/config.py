"""Settings and complete team bundles. No separate process library or model scheduler."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from agilecode.core.types import Settings, Team, AgileCodeError
from agilecode.core.defaults import standard_team
from .store import ProjectStore, decode, encode


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".save-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Configuration:
    def __init__(self, global_root: Path, store: ProjectStore):
        self.root = global_root.expanduser().resolve()
        self.store = store
        self.settings = Settings()
        self.revision = 0

    async def load(self) -> Settings:
        path = self.root / "settings.json"
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("teams", "skills"):
            (self.root / name).mkdir(exist_ok=True)
        base = json.loads(path.read_text()) if path.exists() else Settings().model_dump(mode="json")
        row = await self.store.one("SELECT value FROM settings WHERE key='project'")
        if row:
            base.update(decode(row["value"], {}))
        self.settings = Settings.model_validate(base)
        return self.settings

    async def save(self, settings: Settings, scope="project") -> None:
        if scope == "global":
            atomic_json(self.root / "settings.json", settings.model_dump(mode="json"))
        elif scope == "project":
            await self.store.execute("INSERT INTO settings(key,value) VALUES('project',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (settings.model_dump_json(),))
        else:
            raise AgileCodeError("Scope must be global or project.")
        await self.load()
        self.revision += 1

    async def list_teams(self) -> list[dict]:
        result = [{"ref": "builtin:standard", "scope": "builtin", "team": standard_team().model_dump(mode="json")}]
        for path in sorted((self.root / "teams").glob("*.json")):
            try:
                team = Team.model_validate_json(path.read_text())
                result.append({"ref": f"global:{team.id}", "scope": "global", "team": team.model_dump(mode="json")})
            except Exception as exc:
                result.append({"ref": f"invalid:{path.name}", "scope": "global", "error": str(exc)})
        for row in await self.store.query("SELECT * FROM teams ORDER BY id"):
            team = Team.model_validate_json(row["data"])
            result.append({"ref": f"project:{team.id}", "scope": "project", "team": team.model_dump(mode="json")})
        return result

    async def get_team(self, reference: str) -> Team:
        for item in await self.list_teams():
            if item["ref"] == reference and "team" in item:
                return Team.model_validate(item["team"])
        raise AgileCodeError(f"Team {reference!r} is not available. Choose a team in this project or globally.")

    async def save_team(self, team: Team, scope: str) -> str:
        if scope == "global":
            atomic_json(self.root / "teams" / f"{team.id}.json", team.model_dump(mode="json"))
        elif scope == "project":
            await self.store.execute("INSERT INTO teams(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,revision=revision+1", (team.id, team.model_dump_json()))
        else:
            raise AgileCodeError("Built-in teams are immutable. Save a global or project copy.")
        return f"{scope}:{team.id}"
