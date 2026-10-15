"""The configuration operation: scoped patches, provider identities and upgrades."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from functools import wraps
import hashlib
import json
from pathlib import Path

from agilecode.core.types import (Settings, Team, AgileCodeError, Conflict, LEGACY_ALLOWED_TOOL_GROUPS,
                                  ModelProfile, ProviderConfig, ProviderPool, uid)
from agilecode.core.defaults import requirements_engineering_team, standard_team, systems_architecture_team
from agilecode.adapters.store import ProjectStore, decode, encode
from agilecode.adapters.config_lock import atomic_json, shared_file_lock

_UNSET = object()


def configuration_write(fn):
    @wraps(fn)
    async def invoke(self, *args, **kwargs):
        from agilecode.application.coordination import settle
        async def operation():
            if self.coordinator is None:
                return await fn(self, *args, **kwargs)
            async with self.coordinator.reserve("__configuration__", uid(), "configuration", parent_owner=kwargs.get("owner")):
                return await fn(self, *args, **kwargs)
        return await settle(operation())
    return invoke



def policy_publication(fn):
    """Order persisted settings and their effective publication with dispatch."""
    @wraps(fn)
    async def invoke(self, *args, **kwargs):
        from agilecode.application.policy import policy_lock
        async with policy_lock(self.store):
            return await fn(self, *args, **kwargs)
    return invoke


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else deepcopy(default)


def content_revision(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _require_settings_version(data, scope):
    version = data.get("format_version", 1)
    if version != 1:
        raise AgileCodeError(f"Unsupported {scope} settings format {version}; nothing was migrated.")


def _check(actual, expected, label):
    if actual != expected:
        raise Conflict(f"{label} changed since it was read. Reload before saving.")


def _settings_value(raw):
    base = deepcopy(raw)
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

    return Settings.model_validate(base)


class Configuration:
    def __init__(self, global_root: Path, store: ProjectStore):
        self.root = global_root.expanduser().resolve()
        self.store = store
        self.settings = Settings()
        self.provider_pool = ProviderPool()
        self.revision = 0
        self.coordinator = None
        self.migration_conflicts: list[dict] = []
        self._settings_base = {"global": {}, "project": {}}
        self._effective_base = self.settings.model_dump(mode="json")
        self._team_base: dict[str, dict | None] = {}

    async def needs_migration(self) -> bool:
        """Read-only indication that an explicit configuration upgrade is pending."""
        if not (self.root / "providers.json").exists():
            return True
        global_data = _read(self.root / "settings.json", {})
        if any(key in global_data for key in ("profiles", "active_profile")):
            return True
        table = await self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'")
        if table:
            row = await self.store.one("SELECT value FROM settings WHERE key='project'")
            if row and any(key in decode(row["value"], {}) for key in ("profiles", "active_profile")):
                return True
        return False

    async def check_formats(self):
        """Read-only preflight usable before the SQLite schema is upgraded."""
        _require_settings_version(_read(self.root / "settings.json", {}), "global")
        pool = _read(self.root / "providers.json", None)
        if pool is not None:
            ProviderPool.model_validate(pool)
        table = await self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'")
        if table:
            row = await self.store.one("SELECT value FROM settings WHERE key='project'")
            if row:
                _require_settings_version(decode(row["value"], {}), "project")

    @configuration_write
    @policy_publication
    async def migrate(self, *, owner=None) -> list[dict]:
        """Explicit startup upgrade. Ordinary reads never remove legacy sources.

        Originals are retained separately, including each project's legacy values.
        Conflicting IDs keep their source definitions and cannot silently resolve
        to whichever project happened to open first.
        """
        row = await self.store.one("SELECT value FROM settings WHERE key='project'")
        project = decode(row["value"], {}) if row else {}
        settings_path, pool_path = self.root / "settings.json", self.root / "providers.json"
        conflicts = []
        with ExitStack() as locks:
            for path in sorted((settings_path, pool_path)):
                locks.enter_context(shared_file_lock(path, owner=owner))
            global_data = _read(settings_path, {})
            _require_settings_version(global_data, "global")
            _require_settings_version(project, "project")
            pool_raw = _read(pool_path, None)
            pool = ProviderPool.model_validate(pool_raw) if pool_raw else ProviderPool(providers=[])
            providers = {item.id: item for item in pool.providers}
            sources = [("global", global_data), ("project", project)]
            source_profiles = {}
            for scope, data in sources:
                profiles = data.get("profiles")
                if not isinstance(profiles, dict):
                    continue
                # A content-addressed original is never overwritten on a restart.
                original_root = self.root / "migration-originals" if scope == "global" else self.store.path.parent / "migration-originals"
                original = original_root / ("settings-" + content_revision(data) + ".json")
                if not original.exists():
                    atomic_json(original, data)
                normalized = {}
                for identity, raw in profiles.items():
                    if not isinstance(identity, str) or not isinstance(raw, dict):
                        continue
                    clean = {key: value for key, value in raw.items() if key not in {"max_tokens", "context_window"}}
                    profile = ModelProfile.model_validate(clean)
                    normalized[identity] = profile.model_dump(mode="json")
                    current = providers.get(identity)
                    if current and current.profile.model_dump(mode="json") != normalized[identity]:
                        conflicts.append({"scope": scope, "provider_id": identity, "original": str(original)})
                    elif current is None:
                        providers[identity] = ProviderConfig(id=identity, name=self._provider_name(identity), profile=profile)
                source_profiles[scope] = normalized
            # Also compare original scopes directly; flattening before migration
            # would lose a shadowed global definition.
            for identity in source_profiles.get("global", {}).keys() & source_profiles.get("project", {}).keys():
                if source_profiles["global"][identity] != source_profiles["project"][identity]:
                    if not any(c["scope"] == "project" and c["provider_id"] == identity for c in conflicts):
                        conflicts.append({"scope": "project", "provider_id": identity})
            if not providers and pool_raw is None:
                providers["local"] = ProviderConfig(id="local", name="Local", profile=ModelProfile())
            default = pool.default_provider_id
            if not providers:
                default = None
            elif default not in providers:
                preferred = global_data.get("active_profile") or project.get("active_profile")
                default = preferred if preferred in providers else next(iter(providers))
            new_pool = ProviderPool(default_provider_id=default, providers=list(providers.values()))
            if pool_raw != new_pool.model_dump(mode="json"):
                if pool_raw is not None:
                    original = self.root / "migration-originals" / ("providers-" + content_revision(pool_raw) + ".json")
                    if not original.exists():
                        atomic_json(original, pool_raw)
                atomic_json(pool_path, new_pool.model_dump(mode="json"))
            if any(key in global_data for key in ("profiles", "active_profile")) and not any(c["scope"] == "global" for c in conflicts):
                cleaned = {k: v for k, v in global_data.items() if k not in {"profiles", "active_profile"}}
                atomic_json(settings_path, cleaned)
        if row and any(key in project for key in ("profiles", "active_profile")) and not any(c["scope"] == "project" for c in conflicts):
            cleaned = {k: v for k, v in project.items() if k not in {"profiles", "active_profile"}}
            def remove_legacy(conn):
                current = conn.execute("SELECT value FROM settings WHERE key='project'").fetchone()
                _check(decode(current[0], {}) if current else {}, project, "Project settings")
                conn.execute("UPDATE settings SET value=? WHERE key='project'", (encode(cleaned),))
            await self.store.transaction(remove_legacy)
        self.migration_conflicts = conflicts
        await self._load_unlocked()
        return deepcopy(conflicts)

    @policy_publication
    async def load(self) -> Settings:
        return await self._load_unlocked()

    async def _load_unlocked(self) -> Settings:
        global_data = _read(self.root / "settings.json", {})
        row = await self.store.one("SELECT value FROM settings WHERE key='project'")
        project_data = decode(row["value"], {}) if row else {}
        _require_settings_version(global_data, "global")
        _require_settings_version(project_data, "project")
        self._settings_base = {"global": deepcopy(global_data), "project": deepcopy(project_data)}
        base = {**global_data, **project_data}
        base.pop("profiles", None)
        base.pop("active_profile", None)
        self._load_provider_pool()
        self.settings = _settings_value(base)
        self._effective_base = self.settings.model_dump(mode="json")
        return self.settings

    @policy_publication
    async def snapshots(self) -> dict:
        """One immutable effective/scoped settings view for editing clients.

        Keep the global lock until the project transaction has read its matching
        view. The displayed values and CAS baseline always come from these same
        captured documents, never from mutable self.settings after another await.
        """
        path = self.root / "settings.json"
        with shared_file_lock(path):
            global_data = _read(path, {})
            _require_settings_version(global_data, "global")
            def read_project(conn):
                row = conn.execute("SELECT value FROM settings WHERE key='project'").fetchone()
                return decode(row[0], {}) if row else {}
            project_data = await self.store.transaction(read_project)
            _require_settings_version(project_data, "project")
            settings = _settings_value({**global_data, **project_data})
            effective = settings.model_dump(mode="json")
            values = {"global": global_data, "project": project_data}
            result = {"effective": deepcopy(effective)}
            for scope, document in values.items():
                result[scope] = {"scope": scope, "values": deepcopy(document),
                                 "effective": deepcopy(effective), "revision": content_revision(document)}
            self._settings_base = deepcopy(values)
            self._effective_base = deepcopy(effective)
            self.settings = settings
            return result

    async def snapshot(self, scope="project") -> dict:
        if scope not in {"global", "project"}:
            raise AgileCodeError("Scope must be global or project.")
        return (await self.snapshots())[scope]

    async def save(self, settings: Settings, scope="project", *, expected=_UNSET) -> None:
        desired = settings.model_dump(mode="json")
        if expected is _UNSET:
            expected = {"values": deepcopy(self._settings_base[scope]),
                        "effective": deepcopy(self._effective_base)}
        baseline = expected.get("effective", expected)
        changes = {key: value for key, value in desired.items() if baseline.get(key, _UNSET) != value}
        await self.patch_settings(changes, scope, expected=expected)

    @configuration_write
    @policy_publication
    async def patch_settings(self, changes: dict, scope="project", *, expected=_UNSET) -> dict:
        if scope not in {"global", "project"}:
            raise AgileCodeError("Scope must be global or project.")
        baseline = deepcopy(self._settings_base[scope] if expected is _UNSET else expected.get("values", expected))
        unknown = changes.keys() - Settings.model_fields.keys()
        if unknown:
            raise AgileCodeError("Unknown settings fields: " + ", ".join(sorted(unknown)))
        def patch(current):
            for key in changes:
                _check(current.get(key, _UNSET), baseline.get(key, _UNSET), f"Setting {key!r}")
            updated = {**current, **deepcopy(changes)}
            effective = {**self._settings_base["global"], **updated} if scope == "project" else updated
            # Legacy keys are preserved until explicit migration handles them.
            Settings.model_validate({k: v for k, v in effective.items() if k not in {"profiles", "active_profile", "max_requests_per_attempt"}})
            return updated
        if scope == "global":
            path = self.root / "settings.json"
            with shared_file_lock(path):
                atomic_json(path, patch(_read(path, {})))
        else:
            def write(conn):
                row = conn.execute("SELECT value FROM settings WHERE key='project'").fetchone()
                value = patch(decode(row[0], {}) if row else {})
                conn.execute("INSERT INTO settings(key,value) VALUES('project',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encode(value),))
            await self.store.transaction(write)
        await self._load_unlocked()
        self.revision += 1
        return {"scope": scope, "values": deepcopy(self._settings_base[scope]), "effective": self.settings.model_dump(mode="json"), "revision": content_revision(self._settings_base[scope])}

    @configuration_write
    @policy_publication
    async def reset_project(self, *, expected=_UNSET) -> dict:
        baseline = deepcopy(self._settings_base["project"] if expected is _UNSET else expected.get("values", expected))
        def reset(conn):
            row = conn.execute("SELECT value FROM settings WHERE key='project'").fetchone()
            _check(decode(row[0], {}) if row else {}, baseline, "Project settings")
            conn.execute("DELETE FROM settings WHERE key='project'")
        await self.store.transaction(reset)
        await self._load_unlocked()
        self.revision += 1
        return {"inheriting": "global settings"}

    @staticmethod
    def _provider_name(provider_id: str) -> str:
        return provider_id.replace("_", " ").replace("-", " ").strip().title() or provider_id

    def _load_provider_pool(self, *unused) -> ProviderPool:
        raw = _read(self.root / "providers.json", None)
        self.provider_pool = ProviderPool.model_validate(raw) if raw else ProviderPool()
        return self.provider_pool

    def providers_snapshot(self) -> dict:
        self._load_provider_pool()
        return self.provider_pool.model_dump(mode="json")

    def get_provider(self, provider_id: str | None = None) -> ProviderConfig:
        self._load_provider_pool()
        selected = provider_id or self.provider_pool.default_provider_id
        if any(item["provider_id"] == selected for item in self.migration_conflicts):
            raise Conflict(f"Provider {selected!r} has conflicting preserved legacy definitions. Resolve those definitions before use.")
        if not selected:
            raise AgileCodeError("No provider is configured. Add one to the Provider Pool.")
        for provider in self.provider_pool.providers:
            if provider.id == selected:
                return provider
        raise AgileCodeError(f"Provider {selected!r} is not available in the global Provider Pool.")

    def _provider_expected(self, identity):
        return next((p.model_dump(mode="json") for p in self.provider_pool.providers if p.id == identity), None)

    def _provider_write(self, identity, mutation, *, expected=_UNSET, expected_default=_UNSET, check_default=False):
        if self.coordinator is not None and self.coordinator.maintenance_owner:
            raise Conflict("Project maintenance is in progress. Retry after it finishes.")
        prior = self._provider_expected(identity) if expected is _UNSET else expected
        prior_default = self.provider_pool.default_provider_id if expected_default is _UNSET else expected_default
        path = self.root / "providers.json"
        with shared_file_lock(path):
            pool = ProviderPool.model_validate(_read(path, ProviderPool().model_dump(mode="json")))
            current = next((p.model_dump(mode="json") for p in pool.providers if p.id == identity), None)
            _check(current, prior, f"Provider {identity!r}")
            if check_default:
                _check(pool.default_provider_id, prior_default, "Default provider")
            self.provider_pool = mutation(pool)
            atomic_json(path, self.provider_pool.model_dump(mode="json"))
        self.revision += 1
        return self.provider_pool.model_dump(mode="json")

    def save_provider(self, provider: ProviderConfig, *, make_default=False, expected=_UNSET, expected_default=_UNSET) -> dict:
        def update(pool):
            providers = [provider if item.id == provider.id else item for item in pool.providers]
            if not any(item.id == provider.id for item in pool.providers):
                providers.append(provider)
            default = provider.id if make_default or pool.default_provider_id is None else pool.default_provider_id
            return ProviderPool(default_provider_id=default, providers=providers)
        return self._provider_write(provider.id, update, expected=expected, expected_default=expected_default, check_default=make_default)

    def set_default_provider(self, provider_id: str, *, expected=_UNSET, expected_default=_UNSET) -> dict:
        def update(pool):
            if not any(item.id == provider_id for item in pool.providers):
                raise AgileCodeError(f"Provider {provider_id!r} is not available in the global Provider Pool.")
            return pool.model_copy(update={"default_provider_id": provider_id}, deep=True)
        return self._provider_write(provider_id, update, expected=expected, expected_default=expected_default, check_default=True)

    def delete_provider(self, provider_id: str, *, expected=_UNSET, expected_default=_UNSET) -> dict:
        def update(pool):
            providers = [item for item in pool.providers if item.id != provider_id]
            if len(providers) == len(pool.providers):
                raise AgileCodeError(f"Provider {provider_id!r} is not available in the global Provider Pool.")
            default = providers[0].id if providers else None
            return ProviderPool(default_provider_id=default if pool.default_provider_id == provider_id else pool.default_provider_id, providers=providers)
        return self._provider_write(provider_id, update, expected=expected, expected_default=expected_default, check_default=True)

    async def models(self, client, provider_id=None):
        return await client.models(self.get_provider(provider_id).profile)
    def _catalog_service(self):
        if not hasattr(self, "catalog"):
            from .catalog import CatalogService
            self.catalog = CatalogService(self, None, None)
        return self.catalog

    async def list_teams(self):
        return await self._catalog_service().list_teams()

    async def get_team(self, reference):
        return await self._catalog_service().get_team(reference)

    async def save_team(self, team, scope, *, expected=_UNSET):
        return await self._catalog_service().save_team(team, scope, expected=expected)
