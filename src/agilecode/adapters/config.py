"""Settings and complete team bundles. No separate process library or model scheduler."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from agilecode.core.types import (Settings, Team, AgileCodeError, LEGACY_ALLOWED_TOOL_GROUPS,
                                  ModelProfile, ProviderConfig, ProviderPool)
from agilecode.core.defaults import requirements_engineering_team, standard_team, systems_architecture_team
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
        self.provider_pool = ProviderPool()
        self.revision = 0

    async def load(self) -> Settings:
        path = self.root / "settings.json"
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("teams", "skills"):
            (self.root / name).mkdir(exist_ok=True)

        global_base = json.loads(path.read_text()) if path.exists() else Settings().model_dump(mode="json")
        row = await self.store.one("SELECT value FROM settings WHERE key='project'")
        project_base = decode(row["value"], {}) if row else {}
        base = {**global_base, **project_base}

        # Provider/model configurations used to live inside settings. Migrate the
        # effective legacy profiles into the global Provider Pool, preserving IDs
        # so existing team-member overrides continue to resolve.
        legacy_profiles = base.get("profiles")
        legacy_active = base.get("active_profile")
        if isinstance(legacy_profiles, dict):
            for profile in legacy_profiles.values():
                if isinstance(profile, dict):
                    profile.pop("max_tokens", None)
                    profile.pop("context_window", None)
        else:
            legacy_profiles = None
        self._load_provider_pool(legacy_profiles, legacy_active if isinstance(legacy_active, str) else None)

        # Remove migrated provider fields from their former stores once. Write the
        # remaining raw settings as-is so unrelated legacy migration below can
        # still handle its own keys on this same load.
        global_changed = any(key in global_base for key in ("profiles", "active_profile"))
        global_base.pop("profiles", None)
        global_base.pop("active_profile", None)
        if global_changed:
            atomic_json(path, global_base)
        if row and any(key in project_base for key in ("profiles", "active_profile")):
            project_base.pop("profiles", None)
            project_base.pop("active_profile", None)
            await self.store.execute(
                "UPDATE settings SET value=? WHERE key='project'",
                (encode(project_base),),
            )
        base.pop("profiles", None)
        base.pop("active_profile", None)

        # Older builds imposed a hidden per-attempt model-request cap. The setting
        # no longer exists; discard it so existing settings migrate cleanly.
        base.pop("max_requests_per_attempt", None)

        # Tool categories are no longer part of member/tool authorization. Expand
        # legacy category rules into the concrete native tool names they governed.
        rules = base.get("tool_rules")
        if isinstance(rules, dict):
            rules = dict(rules)
            for group, names in LEGACY_ALLOWED_TOOL_GROUPS.items():
                if group not in rules:
                    continue
                action = rules.pop(group)
                for name in names:
                    current = rules.get(name)
                    if action == "deny" or current is None:
                        rules[name] = action
                # Dynamic MCP aliases cannot be known until discovery. Preserve
                # this one legacy key only as a compatibility fallback in MCPTool.
                if group == "mcp":
                    rules["mcp"] = action
            # Exact rules from the former individual Git tools collapse into
            # the single git_command rule. Preserve an existing git_command rule;
            # otherwise use the most restrictive legacy action when they differed.
            legacy_git = [
                rules.pop(name)
                for name in ("git_status", "git_diff", "git_log")
                if name in rules
            ]
            if legacy_git and "git_command" not in rules:
                rules["git_command"] = (
                    "deny" if "deny" in legacy_git
                    else "ask" if "ask" in legacy_git
                    else "allow"
                )
            base["tool_rules"] = rules

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

    @staticmethod
    def _provider_name(provider_id: str) -> str:
        return provider_id.replace("_", " ").replace("-", " ").strip().title() or provider_id

    def _load_provider_pool(self, legacy_profiles: dict | None = None,
                            legacy_active: str | None = None) -> ProviderPool:
        path = self.root / "providers.json"
        if path.exists():
            pool = ProviderPool.model_validate_json(path.read_text())
            changed = False
        else:
            providers = []
            if legacy_profiles:
                for provider_id, raw in legacy_profiles.items():
                    if not isinstance(provider_id, str) or not isinstance(raw, dict):
                        continue
                    providers.append(ProviderConfig(
                        id=provider_id,
                        name=self._provider_name(provider_id),
                        profile=ModelProfile.model_validate(raw),
                    ))
            if not providers:
                providers = [ProviderConfig(id="local", name="Local", profile=ModelProfile())]
            ids = {provider.id for provider in providers}
            default_id = legacy_active if legacy_active in ids else providers[0].id
            pool = ProviderPool(default_provider_id=default_id, providers=providers)
            changed = True

        if legacy_profiles:
            providers = list(pool.providers)
            known = {provider.id for provider in providers}
            for provider_id, raw in legacy_profiles.items():
                if provider_id in known or not isinstance(provider_id, str) or not isinstance(raw, dict):
                    continue
                providers.append(ProviderConfig(
                    id=provider_id,
                    name=self._provider_name(provider_id),
                    profile=ModelProfile.model_validate(raw),
                ))
                known.add(provider_id)
                changed = True
            default_id = pool.default_provider_id
            if providers and default_id is None:
                default_id = legacy_active if legacy_active in known else providers[0].id
                changed = True
            pool = ProviderPool(default_provider_id=default_id, providers=providers)

        self.provider_pool = pool
        if changed:
            atomic_json(path, pool.model_dump(mode="json"))
        return pool

    def providers_snapshot(self) -> dict:
        return self.provider_pool.model_dump(mode="json")

    def get_provider(self, provider_id: str | None = None) -> ProviderConfig:
        selected = provider_id or self.provider_pool.default_provider_id
        if not selected:
            raise AgileCodeError("No provider is configured. Add one to the Provider Pool.")
        for provider in self.provider_pool.providers:
            if provider.id == selected:
                return provider
        raise AgileCodeError(f"Provider {selected!r} is not available in the global Provider Pool.")

    def save_provider(self, provider: ProviderConfig, *, make_default: bool = False) -> dict:
        providers = list(self.provider_pool.providers)
        for index, current in enumerate(providers):
            if current.id == provider.id:
                providers[index] = provider
                break
        else:
            providers.append(provider)
        default_id = self.provider_pool.default_provider_id
        if make_default or default_id is None:
            default_id = provider.id
        self.provider_pool = ProviderPool(default_provider_id=default_id, providers=providers)
        atomic_json(self.root / "providers.json", self.provider_pool.model_dump(mode="json"))
        self.revision += 1
        return self.providers_snapshot()

    def set_default_provider(self, provider_id: str) -> dict:
        self.get_provider(provider_id)
        self.provider_pool = self.provider_pool.model_copy(update={"default_provider_id": provider_id}, deep=True)
        atomic_json(self.root / "providers.json", self.provider_pool.model_dump(mode="json"))
        self.revision += 1
        return self.providers_snapshot()

    def delete_provider(self, provider_id: str) -> dict:
        providers = [provider for provider in self.provider_pool.providers if provider.id != provider_id]
        if len(providers) == len(self.provider_pool.providers):
            raise AgileCodeError(f"Provider {provider_id!r} is not available in the global Provider Pool.")
        default_id = self.provider_pool.default_provider_id
        if default_id == provider_id:
            default_id = providers[0].id if providers else None
        self.provider_pool = ProviderPool(default_provider_id=default_id, providers=providers)
        atomic_json(self.root / "providers.json", self.provider_pool.model_dump(mode="json"))
        self.revision += 1
        return self.providers_snapshot()

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
