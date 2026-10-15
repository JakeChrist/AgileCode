"""Compatibility entry points for the shared portable backup operations."""
from pathlib import Path
from agilecode.application.services.backup import BackupService
from agilecode.application.services.backup import restore_backup as _restore_backup


def _busy_reason(app):
    pm = app.project_manager
    if pm is not None and pm.state.get("status") == "running":
        return "Pause the Project Manager before creating a backup."
    tasks = list(app.tasks.values()) + list(app.integration_tasks.values())
    processor = getattr(app, "processor", None)
    discussion = getattr(app, "discussion", None)
    engine = processor.agent.engine if processor is not None else getattr(discussion, "_engine", None)
    if engine is not None and engine.is_busy():
        return "Backup requires an idle engine. Finish or stop the active conversation before backing up."
    if any(not task.done() for task in tasks) or (pm is not None and pm.lock.locked()):
        return "Backup requires an idle engine. Stop the current run before backing up interrupted work."
    runtimes = list(app.runtimes.values()) + list(getattr(app.merges, "runtimes", {}).values())
    if any(runtime.shell.has_live_jobs() for runtime in runtimes):
        return "Stop background jobs before backup."
    return None


async def create_backup(app, destination: Path) -> dict:
    service = BackupService(root=app.root, storage=app.storage, global_root=app.global_root,
                            pm_root=app.project_manager.root if app.project_manager is not None else app.storage / "project_manager",
                            store=app.store, git=app.git,
                            checkpoints=app.checkpoints, coordinator=app.coordinator,
                            busy_reason=lambda: _busy_reason(app), ticket_execution=app.ticket_execution)
    return await service.create(destination)


async def restore_backup(source: Path, destination: Path, global_root: Path | None = None) -> dict:
    return await _restore_backup(source, destination, global_root)
