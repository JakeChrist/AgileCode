"""Public headless application API. Desktop and CLI are replaceable clients."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import traceback
from pathlib import Path
from typing import Any

from .types import (AgileCodeError, AccessDenied, Conflict, NeedsReview, GenerationStopped, BoardState, Settings, Team,
                    StepResult, StepContext, Events, Event, uid, now)
from agilecode.adapters.store import ProjectStore, encode, decode
from agilecode.adapters.config import Configuration
from agilecode.adapters.lock import ProjectLock
from agilecode.adapters.git import GitRepository
from agilecode.adapters.workspace import Workspace, digest, inside
from agilecode.adapters.skills import SkillRegistry
from agilecode.adapters.checkpoints import CheckpointService


class Application:
    """One project engine, many isolated concurrent ticket runtimes.

    Construct inside the hosting asyncio loop. No method imports Qt, refers to a
    selected UI ticket, loads a model, or imposes inference-concurrency permits.
    """
    def __init__(self, project: str | Path, global_root: str | Path | None = None, *, client=None, execution: bool = True):
        self.root = Path(project).expanduser().resolve()
        self.global_root = Path(global_root).expanduser().resolve() if global_root else Path.home()/".agilecode"
        self.storage = self.root/".agilecode"
        self.events = Events()
        self.git = GitRepository(self.root)
        self.store = ProjectStore(self.storage/"agilecode.db")
        self.config = Configuration(self.global_root, self.store)
        self.git.operation_timeout = lambda: self.config.settings.git_operation_timeout_seconds
        self.execution_enabled = bool(execution)
        self.client = self.processor = self.merges = None
        self.discussion = self.project_manager = None
        self.skills = SkillRegistry(self.global_root, self.root)
        self.checkpoints = CheckpointService(self.git, self.store)
        self.runtimes, self.tasks, self.integration_tasks = {}, {}, {}
        self.ticket_locks = {}
        self.all_paused = False
        self._all_execution_lock = asyncio.Lock()
        self._deferred_integrations = set()
        self.pending_answers = {}
        self.owner: ProjectLock | None = None
        self.opened = False
        self.closed = False
        self.migration_backup = None
        from agilecode.application.coordination import ProjectCoordinator
        from agilecode.application.query_views import ProjectQueries
        from agilecode.application.services.catalog import CatalogService
        from agilecode.application.services.artifacts import ArtifactService
        from agilecode.application.services.jobs import JobService
        from .pm_profiles import PMProfileLibrary
        self.coordinator = ProjectCoordinator()
        self.config.coordinator = self.coordinator
        self.project_queries = ProjectQueries(self.store, lambda: self.config.settings.conversation_page_size)
        self.profiles = PMProfileLibrary(self.global_root)
        self.profiles.coordinator = self.coordinator
        self.catalog = CatalogService(self.config, self.skills, self.profiles)
        self.artifacts = ArtifactService(self.store, self.storage / "artifacts")
        self.jobs = JobService(self.store, self.artifacts, lambda: list(self.runtimes.values()) +
                               (list(self.merges.runtimes.values()) if self.merges else []))
        from .discussion import ProjectDiscussion
        self.discussion = ProjectDiscussion(self)
        if self.execution_enabled:
            self._initialize_runtime(client)
        self._wire_shared_operations()

    def _initialize_runtime(self, client=None):
        """Execution-capable host composition; definition-only hosts never call it."""
        from .runner import ProcessRunner
        from agilecode.adapters.model import OpenAICompatibleClient
        from .project_manager import ProjectManager
        self.client = client or OpenAICompatibleClient()
        if isinstance(self.client, OpenAICompatibleClient):
            self.client.operational_settings = lambda: self.config.settings
        self.processor = ProcessRunner(self.store, self.git, self.client, self.skills, self.checkpoints,
                                       self.events, lambda: self.config.settings, self.config.get_provider,
                                       self._wait, self._boundary, self.create_ticket)
        self.project_manager = ProjectManager(self)
        self.project_manager.profiles = self.profiles

    def _wire_shared_operations(self):
        from agilecode.application.queries import TicketQueries
        from agilecode.application.tickets import TicketOperations
        from agilecode.application.execution import ExecutionOperations
        from agilecode.application.services.workspace import WorkspaceOperations
        async def source_for_ticket(identity):
            return await self.discussion.source_for_ticket(identity) if self.discussion else None
        self.ticket_queries = TicketQueries(self.store, tasks=self.tasks,
            source_for_ticket=source_for_ticket, controls=lambda: self.execution_ops.execution_controls(),
            activities=lambda ticket_id: tuple(self.coordinator.activities.get(ticket_id, ())))
        self.tickets = TicketOperations(self.store, self.config, self.events, self.coordinator,
            self.ticket_queries, discussion=self.discussion)
        self.execution_ops = ExecutionOperations(root=self.root, global_root=self.global_root,
            storage=self.storage, store=self.store, git=self.git, config=self.config, events=self.events,
            coordinator=self.coordinator, queries=self.ticket_queries, tickets=self.tickets,
            checkpoints=self.checkpoints, processor=self.processor, skills=self.skills, client=self.client,
            discussion=self.discussion, project_manager=self.project_manager,
            execution_enabled=self.execution_enabled)
        self.tickets.execution = self.execution_ops
        for name in ("runtimes", "tasks", "integration_tasks", "ticket_locks", "pending_answers",
                     "_deferred_integrations", "_all_execution_lock"):
            setattr(self, name, getattr(self.execution_ops, name))
        if self.execution_enabled:
            from .integration import MergeCoordinator
            self.merges = MergeCoordinator(self.execution_ops)
            self.execution_ops.merges = self.merges
        self.workspace_ops = WorkspaceOperations(store=self.store, git=self.git, config=self.config,
            root=self.root, global_root=self.global_root, coordinator=self.coordinator,
            queries=self.ticket_queries, runtimes=self.runtimes, tasks=self.tasks,
            checkpoints=self.checkpoints, events=self.events)
        from agilecode.application.source_queries import SourceQueries
        self.source_queries = SourceQueries(store=self.store, git=self.git, root=self.root,
            global_root=self.global_root, tickets=self.ticket_queries, workspace=self.workspace_ops)

    @property
    def all_paused(self):
        return self.coordinator.paused if hasattr(self, "coordinator") else getattr(self, "_all_paused", False)

    @all_paused.setter
    def all_paused(self, value):
        self._all_paused = bool(value)
        if hasattr(self, "coordinator"):
            self.coordinator.paused = bool(value)

    async def open(self, *, initialize=False) -> dict:
        identity = await self.git.identify()
        exists = (self.storage/"agilecode.db").exists()
        if not exists and not initialize:
            raise AgileCodeError("This repository is not initialized. Use 'agilecode init PATH' or Open Project in the desktop app.")
        self.storage.mkdir(parents=True, exist_ok=True)
        self.owner = ProjectLock(self.storage/"engine.lock")
        try:
            await self.store.open(migrate=False)
            await self.config.check_formats()
            # Inspect repository ownership before changing an existing schema.
            has_meta = await self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'")
            prior = await self.store.one("SELECT value FROM meta WHERE key='project'") if has_meta else None
            if prior and decode(prior["value"])["common_git_dir"] != identity["common_git_dir"]:
                raise Conflict("Stored Git identity differs. Use the explicit backup relocation workflow rather than reusing this state in an unrelated repository.")
            from agilecode.application.queries import migrate_run_selection
            self.migration_backup = await self.store.upgrade_schema(data_migrations=(migrate_run_selection,))
            if not prior:
                identity["id"] = uid()
                await self.store.execute("INSERT INTO meta VALUES('project',?)", (encode(identity),))
            ignored_changed = await self.git.ensure_ignore() if initialize else False
            # A current schema can still contain older settings/discussion data.
            legacy_discussion = await self.store.one(
                "SELECT 1 FROM messages WHERE session_id='project-discussion' LIMIT 1")
            promoted = await self.store.one(
                "SELECT 1 FROM discussions WHERE session_id='project-discussion'")
            if self.migration_backup is None and (await self.config.needs_migration() or (legacy_discussion and not promoted)):
                directory = self.storage / "migration-backups"
                directory.mkdir(parents=True, exist_ok=True)
                snapshot = directory / ("data-" + uid() + ".db")
                await self.store.backup(snapshot)
                self.migration_backup = str(snapshot)
            from agilecode.application.services.maintenance import MaintenanceService
            from agilecode.application.coordination import settle
            self.maintenance = MaintenanceService(self.storage, self.global_root, self.coordinator,
                extra_paths=(Path(identity["common_git_dir"]) / "info" / "exclude",))
            async with self.maintenance.startup(database_backup=self.migration_backup) as migration:
                await settle(self._migrate_resources(migration.owner))
            self.migration_snapshot = str(migration.path) if migration.path else None
            controls = await self.store.one("SELECT value FROM meta WHERE key='execution_controls'")
            if controls:
                saved = decode(controls["value"], {})
                self.all_paused = bool(saved.get("all_paused", False))
                self._deferred_integrations.clear()
                self._deferred_integrations.update(saved.get("deferred_integrations", []))
            self.coordinator.blocked_tickets.update(await self.jobs.recover_unowned())
            await self.workspace_ops.recover_pending_operations()
            if self.execution_enabled:
                await self.project_manager.open(migrate=False)
                await self._recover()
                await self.execution_ops.recover_pending_operations()
            else:
                await self.execution_ops.load_interrupted_blockers()
            self.opened = True
            return {"project": str(self.root), "initialized": not exists, "gitignore_changed": ignored_changed,
                    "notice": "Commit the .gitignore change before starting a clean-base ticket." if ignored_changed else ""}
        except BaseException:
            await self.store.close()
            self.owner.close()
            raise

    async def _migrate_resources(self, owner):
        from agilecode.application.discussions import promote_legacy_discussion
        await self.config.migrate(owner=owner)
        await promote_legacy_discussion(self.store)
        if self.execution_enabled:
            await self.catalog.install_bundled(owner=owner)
            await self.project_manager.migrate_storage()

    async def _install_builtin_skills(self):
        return await self.catalog.install_skills()

    async def _recover(self):
        from agilecode.application.recovery import recover_project
        return await recover_project(self.execution_ops)

    def _ticket_lock(self, *args, **kwargs):
        return self.execution_ops._ticket_lock(*args, **kwargs)

    async def tool_catalog(self):
        return await self.catalog.tool_catalog()


    @staticmethod
    def _epic_id(value):
        from agilecode.application.tickets import TicketOperations
        return TicketOperations._epic_id(value)

    async def create_epic(self, *args, **kwargs):
        return await self.tickets.create_epic(*args, **kwargs)

    async def list_epics(self, *args, **kwargs):
        return await self.tickets.list_epics(*args, **kwargs)

    async def get_epic(self, *args, **kwargs):
        return await self.tickets.get_epic(*args, **kwargs)

    async def update_epic(self, *args, **kwargs):
        return await self.tickets.update_epic(*args, **kwargs)

    async def create_ticket(self, *args, **kwargs):
        return await self.tickets.create_ticket(*args, **kwargs)

    async def find_ticket(self, *args, **kwargs):
        return await self.tickets.find_ticket(*args, **kwargs)

    async def edit_ticket(self, *args, **kwargs):
        return await self.tickets.edit_ticket(*args, **kwargs)

    async def add_dependency(self, *args, **kwargs):
        return await self.tickets.add_dependency(*args, **kwargs)

    async def remove_dependency(self, *args, **kwargs):
        return await self.tickets.remove_dependency(*args, **kwargs)

    async def move_ticket(self, *args, **kwargs):
        return await self.tickets.move_ticket(*args, **kwargs)

    async def archive_ticket(self, *args, **kwargs):
        return await self.tickets.archive_ticket(*args, **kwargs)

    async def delete_ticket(self, *args, **kwargs):
        return await self.execution_ops.delete_ticket(*args, **kwargs)

    async def board(self, *args, **kwargs):
        return await self.ticket_queries.board(*args, **kwargs)

    async def latest_run(self, *args, **kwargs):
        return await self.ticket_queries.latest_run(*args, **kwargs)

    async def active_run(self, *args, **kwargs):
        return await self.ticket_queries.active_run(*args, **kwargs)

    async def execution_status(self, *args, **kwargs):
        return await self.ticket_queries.execution_status(*args, **kwargs)

    async def ticket_detail(self, *args, **kwargs):
        return await self.ticket_queries.ticket_detail(*args, **kwargs)

    async def conversation(self, *args, **kwargs):
        return await self.project_queries.conversation(*args, **kwargs)

    async def start_ticket(self, *args, **kwargs):
        return await self.execution_ops.start_ticket(*args, **kwargs)

    def _runtime(self, *args, **kwargs):
        return self.execution_ops._runtime(*args, **kwargs)

    async def _complete_read_only_run(self, *args, **kwargs):
        return await self.execution_ops._complete_read_only_run(*args, **kwargs)

    async def _boundary(self, *args, **kwargs):
        return await self.execution_ops._boundary(*args, **kwargs)

    async def _run(self, *args, **kwargs):
        return await self.execution_ops._run(*args, **kwargs)

    async def wait_for_run(self, *args, **kwargs):
        return await self.execution_ops.wait_for_run(*args, **kwargs)

    async def stop_generation(self, *args, **kwargs):
        return await self.execution_ops.stop_generation(*args, **kwargs)

    def execution_controls(self, *args, **kwargs):
        return self.execution_ops.execution_controls(*args, **kwargs)

    def _require_execution_enabled(self, *args, **kwargs):
        return self.execution_ops._require_execution_enabled(*args, **kwargs)

    async def _persist_execution_controls(self, *args, **kwargs):
        return await self.execution_ops._persist_execution_controls(*args, **kwargs)

    async def _defer_integration(self, *args, **kwargs):
        return await self.execution_ops._defer_integration(*args, **kwargs)

    async def pause_all(self, *args, **kwargs):
        return await self.execution_ops.pause_all(*args, **kwargs)

    async def resume_all(self, *args, **kwargs):
        return await self.execution_ops.resume_all(*args, **kwargs)

    async def pause_run(self, *args, **kwargs):
        return await self.execution_ops.pause_run(*args, **kwargs)

    async def _pause_run(self, *args, **kwargs):
        return await self.execution_ops._pause_run(*args, **kwargs)

    async def resume_run(self, *args, **kwargs):
        return await self.execution_ops.resume_run(*args, **kwargs)

    async def _resume_run(self, *args, **kwargs):
        return await self.execution_ops._resume_run(*args, **kwargs)

    async def cancel_run(self, *args, **kwargs):
        return await self.execution_ops.cancel_run(*args, **kwargs)

    async def _wait(self, *args, **kwargs):
        return await self.execution_ops._wait(*args, **kwargs)

    async def answer(self, *args, **kwargs):
        return await self.execution_ops.answer(*args, **kwargs)

    async def submit_feedback(self, *args, **kwargs):
        return await self.execution_ops.submit_feedback(*args, **kwargs)

    async def prepare_integration(self, *args, **kwargs):
        return await self.execution_ops.prepare_integration(*args, **kwargs)

    async def approve_integration(self, *args, **kwargs):
        return await self.execution_ops.approve_integration(*args, **kwargs)

    async def retry_cleanup(self, *args, **kwargs):
        return await self.execution_ops.retry_cleanup(*args, **kwargs)

    async def checkpoint(self, *args, **kwargs):
        return await self.workspace_ops.checkpoint(*args, **kwargs)

    async def restore_checkpoint(self, *args, **kwargs):
        return await self.workspace_ops.restore_checkpoint(*args, **kwargs)

    async def read_source(self, *args, **kwargs):
        return await self.workspace_ops.read_source(*args, **kwargs)

    async def save_source(self, *args, **kwargs):
        return await self.workspace_ops.save_source(*args, **kwargs)

    async def delete_attempt(self, *args, **kwargs):
        return await self.execution_ops.delete_attempt(*args, **kwargs)

    async def step_diff(self, *args, **kwargs):
        return await self.source_queries.step_diff(*args, **kwargs)

    async def diff(self, *args, **kwargs):
        return await self.source_queries.diff(*args, **kwargs)

    async def backup(self, destination: str | Path):
        from .backup import create_backup
        return await create_backup(self, Path(destination))

    async def export_ticket(self, ticket_id, destination):
        from agilecode.application.services.backup import export_ticket
        return await export_ticket(ticket_id, destination, ticket_detail=self.ticket_detail,
                                   messages=self.store.messages)

    async def job_output(self, *args, **kwargs):
        return await self.jobs.output(*args, **kwargs)

    async def stop_job(self, *args, **kwargs):
        return await self.jobs.stop(*args, **kwargs)

    async def send_stdin(self, *args, **kwargs):
        return await self.jobs.send_input(*args, **kwargs)

    async def selected_run(self, *args, **kwargs):
        return await self.ticket_queries.selected_run(*args, **kwargs)

    async def ticket_execution(self, *args, **kwargs):
        return await self.ticket_queries.ticket_execution(*args, **kwargs)

    async def conversation_page(self, *args, **kwargs):
        return await self.project_queries.conversation_page(*args, **kwargs)

    async def exact_requests(self, *args, **kwargs):
        return await self.project_queries.exact_requests(*args, **kwargs)

    async def pending_waits(self, *args, **kwargs):
        return await self.project_queries.pending_waits(*args, **kwargs)

    async def get_wait(self, *args, **kwargs):
        return await self.project_queries.get_wait(*args, **kwargs)

    async def preview_checkpoint(self, *args, **kwargs):
        return await self.workspace_ops.preview_checkpoint(*args, **kwargs)

    async def source_view(self, *args, **kwargs):
        return await self.source_queries.code(*args, **kwargs)

    async def file_list(self, *args, **kwargs):
        return await self.source_queries.file_list(*args, **kwargs)

    async def compare_file(self, *args, **kwargs):
        return await self.source_queries.compare_file(*args, **kwargs)

    async def integration_diff(self, *args, **kwargs):
        return await self.source_queries.integration_diff(*args, **kwargs)

    async def models(self, provider_id=None, *, profile=None):
        from agilecode.adapters.model import OpenAICompatibleClient
        temporary = self.client is None
        client = self.client or OpenAICompatibleClient()
        try:
            if profile is not None:
                return await client.models(profile)
            return await self.config.models(client, provider_id)
        finally:
            if temporary:
                await client.close()

    async def close(self):
        """Keep writable ownership until every admitted operation really settles."""
        if self.closed:
            return
        from agilecode.application.coordination import settle
        task = getattr(self, "_closing_task", None)
        if task is None:
            task = self._closing_task = asyncio.create_task(self._close_owned())
        await settle(task)

    async def _close_owned(self):
        await self.coordinator.close_admission()
        if self.project_manager is not None:
            await self.project_manager.shutdown()
        if self.discussion is not None:
            await self.discussion.shutdown()
        for runtime in list(self.runtimes.values()):
            runtime.control.permit.set()
        # Includes manual integration, source writes, configuration, backup and
        # cancellation-resistant child operations absent from the old task maps.
        await self.coordinator.drain_owned_work()
        # Spawned tasks stay owned through their post-worker continuations.
        # Do not cancel protected Git/file activity through an older task map.
        await asyncio.gather(*self.tasks.values(), *self.integration_tasks.values(),
                             return_exceptions=True)
        for runtime in list(self.runtimes.values()):
            await runtime.shell.close()
            await runtime.mcp.close()
        if self.merges is not None:
            await self.merges.close()
        if self.client is not None:
            await self.client.close()
        await self.store.close()
        if self.owner:
            self.owner.close()
        self.closed = True
