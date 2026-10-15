from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from agilecode.adapters.config import Configuration
from agilecode.adapters.config_lock import shared_file_lock
from agilecode.adapters.skills import SkillRegistry
from agilecode.adapters.store import ProjectStore, encode
from agilecode.application.coordination import ProjectCoordinator
from agilecode.application.services.artifacts import ArtifactService
from agilecode.application.services.catalog import CatalogService, install_builtin_skills
from agilecode.application.services.jobs import JobService
from agilecode.core.defaults import standard_team
from agilecode.core.pm_profiles import PMProfileLibrary
from agilecode.core.types import AccessDenied, Conflict, ModelProfile, ProviderConfig, now


async def config(root, database):
    store = ProjectStore(database)
    await store.open()
    result = Configuration(root, store)
    await result.migrate()
    return result


def provider(identity, model=None):
    return ProviderConfig(id=identity, name=identity, profile=ModelProfile(model=model))


@pytest.mark.asyncio
async def test_provider_disjoint_changes_and_stale_same_entry(tmp_path):
    a = await config(tmp_path / 'global', tmp_path / 'a' / 'db')
    b = await config(tmp_path / 'global', tmp_path / 'b' / 'db')
    try:
        a.save_provider(provider('one'))
        b.save_provider(provider('two'))
        assert {p['id'] for p in b.providers_snapshot()['providers']} == {'local', 'one', 'two'}
        before = a.providers_snapshot()['providers'][0]
        b.providers_snapshot()
        a.save_provider(provider('local', 'new'), expected=before)
        with pytest.raises(Conflict):
            b.save_provider(provider('local', 'stale'), expected=before)
        assert a.get_provider('local').profile.model == 'new'
    finally:
        await a.store.close(); await b.store.close()


@pytest.mark.asyncio
async def test_settings_patch_merge_stale_and_project_reset(tmp_path):
    a = await config(tmp_path / 'global', tmp_path / 'a' / 'db')
    b = await config(tmp_path / 'global', tmp_path / 'b' / 'db')
    try:
        old_a, old_b = await a.snapshot('global'), await b.snapshot('global')
        await a.patch_settings({'max_corrections': 4}, 'global', expected=old_a)
        await b.patch_settings({'merge_on_completion': False}, 'global', expected=old_b)
        assert (await b.load()).max_corrections == 4
        with pytest.raises(Conflict):
            await b.patch_settings({'max_corrections': 8}, 'global', expected=old_b)
        await b.patch_settings({'max_corrections': 9}, 'project')
        revision = b.revision
        await b.reset_project()
        assert b.revision == revision + 1
        assert b.settings.max_corrections == 4
        assert b.settings.merge_on_completion is False
    finally:
        await a.store.close(); await b.store.close()


@pytest.mark.asyncio
async def test_load_is_pure_and_conflicting_provider_originals_survive(tmp_path):
    root = tmp_path / 'global'; root.mkdir()
    global_legacy = {'profiles': {'same': {'model': 'global'}}, 'active_profile': 'same'}
    project_legacy = {'profiles': {'same': {'model': 'project'}}, 'active_profile': 'same'}
    original = json.dumps(global_legacy)
    (root / 'settings.json').write_text(original)
    store = ProjectStore(tmp_path / 'project' / 'db'); await store.open()
    await store.execute("INSERT INTO settings VALUES('project',?)", (encode(project_legacy),))
    c = Configuration(root, store)
    try:
        await c.load()
        assert (root / 'settings.json').read_text() == original
        assert not (root / 'providers.json').exists()
        conflicts = await c.migrate()
        assert conflicts and conflicts[0]['provider_id'] == 'same'
        assert list((root / 'migration-originals').glob('settings-*.json'))
        assert list((store.path.parent / 'migration-originals').glob('settings-*.json'))
        assert json.loads((await store.one("SELECT value FROM settings WHERE key='project'"))['value']) == project_legacy
        with pytest.raises(Conflict):
            c.get_provider('same')
        c2 = Configuration(root, store)
        assert await c2.migrate()
        with pytest.raises(Conflict):
            c2.get_provider('same')
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_equal_legacy_profiles_migrate_without_shadow_loss(tmp_path):
    root = tmp_path / 'global'; root.mkdir()
    raw = {'profiles': {'same': {'model': 'shared', 'context_window': 1234}}, 'active_profile': 'same'}
    (root / 'settings.json').write_text(json.dumps(raw))
    c = await config(root, tmp_path / 'p' / 'db')
    try:
        assert c.get_provider('same').profile.model == 'shared'
        assert 'profiles' not in json.loads((root / 'settings.json').read_text())
        assert not await c.migrate()
    finally:
        await c.store.close()


@pytest.mark.asyncio
async def test_catalog_teams_compare_expected_snapshot(tmp_path):
    a = await config(tmp_path / 'g', tmp_path / 'a' / 'db')
    b = await config(tmp_path / 'g', tmp_path / 'b' / 'db')
    try:
        team = standard_team().model_copy(update={'id': 'custom'})
        await a.save_team(team, 'global')
        old = (await b.get_team('global:custom')).model_dump(mode='json')
        await a.save_team(team.model_copy(update={'name': 'new'}), 'global')
        with pytest.raises(Conflict):
            await b.save_team(team.model_copy(update={'name': 'stale'}), 'global', expected=old)
        assert (await b.get_team('global:custom')).name == 'new'
    finally:
        await a.store.close(); await b.store.close()


def test_profile_stale_edit_and_installer_policy(tmp_path):
    first, second = PMProfileLibrary(tmp_path), PMProfileLibrary(tmp_path)
    first.install_bundled()
    existing = first.list()[0]
    old = second.get(existing['id']).model_dump(mode='json')
    first.save({**existing, 'instructions': 'custom'})
    with pytest.raises(Conflict):
        second.save({**old, 'instructions': 'stale'}, expected=old)
    first.install_bundled()
    assert first.get(existing['id']).instructions == 'custom'


def test_live_skills_and_backup_overlay_policy(tmp_path):
    source, global_root, project_root = tmp_path / 'bundle', tmp_path / 'g', tmp_path / 'p'
    folder = source / 'example'; folder.mkdir(parents=True)
    text = '---\nname: example\ndescription: Example skill\n---\n'
    (folder / 'SKILL.md').write_text(text + 'initial')
    install_builtin_skills(global_root, source)
    registry = SkillRegistry(global_root, project_root)
    first = registry.load('example', ['example'])
    target = global_root / 'skills' / 'example'
    (target / 'SKILL.md').write_text(text + 'custom')
    (target / 'extra.txt').write_text('preserved')
    second = registry.load('example', ['example'])
    assert first['hash'] != second['hash'] and second['instructions'] == 'custom'
    install_builtin_skills(global_root, source)
    backups = list((global_root / 'skill-backups').glob('example-*'))
    assert len(backups) == 1 and (backups[0] / 'SKILL.md').read_text().endswith('custom')
    assert (target / 'extra.txt').read_text() == 'preserved'
    override = project_root / '.agilecode' / 'skills' / 'example'; override.mkdir(parents=True)
    (override / 'SKILL.md').write_text('invalid')
    assert registry.load('example', ['example'])['scope'] == 'global'
    (override / 'SKILL.md').write_text(text + 'project')
    assert registry.load('example', ['example'])['scope'] == 'project'


async def artifact_store(tmp_path):
    store = ProjectStore(tmp_path / 'db'); await store.open()
    for identity in ('a', 'b'):
        await store.insert('tickets', {'id': identity, 'key': identity, 'title': identity, 'description': '',
            'state': 'Backlog', 'team_ref': 'builtin:standard', 'process_id': 'standard', 'created': now(), 'updated': now()})
    return store


@pytest.mark.asyncio
async def test_artifact_byte_pages_growth_capture_truncation_and_scope(tmp_path):
    store = await artifact_store(tmp_path)
    service = ArtifactService(store)
    try:
        artifact = await service.create('a', 'test', content='éabc'.encode())
        first = await service.read(artifact['id'], 0, 1, ticket_id='a')
        assert first['bytes'] == 1 and first['remaining'] and first['total_bytes'] == 5
        assert first['next_offset'] == 1
        end = await service.read(artifact['id'], 5, 10, ticket_id='a')
        assert end['bytes'] == 0 and not end['remaining'] and end['producer_complete']
        beyond = await service.read(artifact['id'], 20, 10, ticket_id='a')
        assert beyond['bytes'] == 0 and not beyond['remaining']
        with pytest.raises(AccessDenied):
            await service.read(artifact['id'], ticket_id='b')
        await service.finalize(artifact['id'], capture_truncated=True)
        assert (await service.read(artifact['id']))['capture_truncated']
        growing = await service.create('a', 'growing')
        with Path(growing['path']).open('ab') as out:
            out.write(b'abc')
        page = await service.read(growing['id'], 0, 3)
        assert not page['producer_complete'] and not page['remaining']
        with Path(growing['path']).open('ab') as out:
            out.write(b'def')
        assert (await service.read(growing['id'], page['next_offset']))['content'] == 'def'
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_jobs_reject_wrong_scope_and_unowned_live_handles(tmp_path):
    store = await artifact_store(tmp_path)
    artifacts = ArtifactService(store)
    try:
        artifact = await artifacts.create('a', 'log', content=b'ok')
        await store.insert('jobs', {'id': 'job', 'ticket_id': 'a', 'command': 'true', 'cwd': str(tmp_path),
                                   'status': 'running', 'artifact_id': artifact['id'], 'created': now()})
        jobs = JobService(store, artifacts)
        with pytest.raises(AccessDenied):
            await jobs.output('job', ticket_id='b')
        with pytest.raises(Exception, match='no longer owns'):
            await jobs.stop('job', ticket_id='a')
        assert (await jobs.output('job', ticket_id='a'))['output'] == 'ok'
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_maintenance_blocks_configuration_writers(tmp_path):
    c = await config(tmp_path / 'g', tmp_path / 'p' / 'db')
    c.coordinator = ProjectCoordinator()
    try:
        async with c.coordinator.maintenance('backup'):
            with pytest.raises(Conflict):
                c.save_provider(provider('blocked'))
            with pytest.raises(Conflict):
                await c.patch_settings({'max_corrections': 1})
        await c.patch_settings({'max_corrections': 1})
    finally:
        await c.store.close()


def _process_save_provider(root, identity, ready, proceed, results):
    try:
        c = Configuration(Path(root), None)
        c.providers_snapshot()
        ready.set()
        assert proceed.wait(10)
        c.save_provider(provider(identity))
        results.put(None)
    except BaseException as exc:
        results.put(repr(exc))


@pytest.mark.asyncio
async def test_two_project_processes_preserve_disjoint_global_entries(tmp_path):
    c = await config(tmp_path / 'g', tmp_path / 'p' / 'db')
    await c.store.close()
    ctx = multiprocessing.get_context('spawn')
    results = ctx.Queue()
    ready = [ctx.Event(), ctx.Event()]; proceed = [ctx.Event(), ctx.Event()]
    workers = [ctx.Process(target=_process_save_provider, args=(str(tmp_path / 'g'), str(i), ready[i], proceed[i], results)) for i in range(2)]
    for process in workers: process.start()
    try:
        assert all(event.wait(10) for event in ready)
        proceed[0].set(); assert results.get(timeout=10) is None
        proceed[1].set(); assert results.get(timeout=10) is None
        assert {p['id'] for p in c.providers_snapshot()['providers']} == {'local', '0', '1'}
    finally:
        for process in workers:
            process.join(10)
            if process.is_alive(): process.terminate(); process.join()


@pytest.mark.asyncio
async def test_migration_snapshot_rolls_back_only_unchanged_owned_global_entries(tmp_path):
    from agilecode.application.services.maintenance import MaintenanceService
    root = tmp_path / 'g'; root.mkdir()
    original = {'profiles': {'imported': {'model': 'legacy'}}, 'active_profile': 'imported'}
    (root / 'settings.json').write_text(json.dumps(original))
    store = ProjectStore(tmp_path / 'project' / '.agilecode' / 'db'); await store.open()
    c = Configuration(root, store); c.coordinator = ProjectCoordinator()
    maintenance = MaintenanceService(store.path.parent, root, c.coordinator)
    try:
        async with maintenance.startup() as migration:
            await c.migrate(owner=migration.owner)
        assert migration.manifest['unrecorded_changes'] == []
        # A second project's later edits must survive rollback of this migration.
        c.save_provider(provider('later', 'project-b'), expected=None)
        await c.patch_settings({'max_corrections': 6}, 'global')
        result = await maintenance.rollback(migration.path)
        assert result['restored'] and not result['conflicts']
        settings = json.loads((root / 'settings.json').read_text())
        assert settings['profiles'] == original['profiles'] and settings['max_corrections'] == 6
        pool = json.loads((root / 'providers.json').read_text())
        assert [item['id'] for item in pool['providers']] == ['later']
        assert pool['default_provider_id'] == 'later'
        # Repeated rollback does not undo a different project's remaining values.
        await maintenance.rollback(migration.path)
        assert json.loads((root / 'providers.json').read_text()) == pool
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_migration_rollback_preserves_later_conflicting_provider_edit(tmp_path):
    from agilecode.application.services.maintenance import MaintenanceService
    root = tmp_path / 'g'; root.mkdir()
    (root / 'settings.json').write_text(json.dumps({'profiles': {'same': {'model': 'old'}}}))
    store = ProjectStore(tmp_path / 'p' / '.agilecode' / 'db'); await store.open()
    c = Configuration(root, store); c.coordinator = ProjectCoordinator()
    maintenance = MaintenanceService(store.path.parent, root, c.coordinator)
    try:
        async with maintenance.startup() as migration:
            await c.migrate(owner=migration.owner)
        c.save_provider(provider('same', 'newer-edit'))
        result = await maintenance.rollback(migration.path)
        assert any('providers.same' in item for item in result['conflicts'])
        assert c.get_provider('same').profile.model == 'newer-edit'
    finally:
        await store.close()


def _crash_configuration_migration(root, storage):
    import os
    from agilecode.application.services.maintenance import MaintenanceService
    async def run():
        store = ProjectStore(Path(storage) / 'db'); await store.open()
        c = Configuration(Path(root), store); c.coordinator = ProjectCoordinator()
        maintenance = MaintenanceService(Path(storage), Path(root), c.coordinator)
        async with maintenance.startup() as migration:
            await c.migrate(owner=migration.owner)
            os._exit(71)
    asyncio.run(run())


@pytest.mark.asyncio
async def test_abrupt_migration_stop_uses_durable_file_intents_twice(tmp_path):
    from agilecode.application.services.maintenance import MaintenanceService
    root, storage = tmp_path / 'g', tmp_path / 'p' / '.agilecode'
    root.mkdir()
    original = {'profiles': {'same': {'model': 'old'}}, 'active_profile': 'same'}
    (root / 'settings.json').write_text(json.dumps(original))
    ctx = multiprocessing.get_context('spawn')
    process = ctx.Process(target=_crash_configuration_migration, args=(str(root), str(storage)))
    process.start(); process.join(10)
    assert process.exitcode == 71
    snapshot = next((storage / 'migration-rollbacks').iterdir())
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    assert manifest['after'] is None and manifest['intents']
    assert (root / 'providers.json').exists()
    for _ in range(2):
        maintenance = MaintenanceService(storage, root, ProjectCoordinator())
        result = await maintenance.rollback(snapshot)
        assert not result['conflicts']
        assert json.loads((root / 'settings.json').read_text()) == original
        assert not (root / 'providers.json').exists()


@pytest.mark.asyncio
async def test_catalog_install_nested_migration_preserves_custom_profile_and_snapshots(tmp_path):
    from agilecode.application.services.maintenance import MaintenanceService
    c = await config(tmp_path / 'g', tmp_path / 'p' / '.agilecode' / 'db')
    c.coordinator = ProjectCoordinator()
    profiles = PMProfileLibrary(c.root)
    catalog = CatalogService(c, SkillRegistry(c.root, tmp_path / 'p'), profiles)
    maintenance = MaintenanceService(c.store.path.parent, c.root, c.coordinator)
    try:
        async with maintenance.startup() as migration:
            await c.migrate(owner=migration.owner)
            await catalog.install_bundled(owner=migration.owner)
        assert profiles.list()
        assert migration.manifest['unrecorded_changes'] == []
        result = await maintenance.rollback(migration.path)
        assert not result['conflicts']
        assert not profiles.list()
    finally:
        await c.store.close()


def test_profile_explicit_absence_is_not_no_expectation(tmp_path):
    profiles = PMProfileLibrary(tmp_path)
    saved = profiles.save({'name': 'Custom', 'instructions': '', 'skills': []}, expected=None)
    with pytest.raises(Conflict):
        profiles.save({**saved, 'name': 'replacement'}, expected=None)
    with pytest.raises(Conflict):
        profiles.delete(saved['id'], expected=None)
    assert profiles.get(saved['id']).name == 'Custom'


@pytest.mark.asyncio
async def test_noop_migration_does_not_accumulate_snapshots(tmp_path):
    from agilecode.application.services.maintenance import MaintenanceService
    c = await config(tmp_path / 'g', tmp_path / 'p' / '.agilecode' / 'db')
    c.coordinator = ProjectCoordinator()
    maintenance = MaintenanceService(c.store.path.parent, c.root, c.coordinator)
    try:
        # Creating lock parents counts as topology creation only on first capture;
        # the read-only scope itself has no file changes.
        async with maintenance.startup() as first:
            await c.migrate(owner=first.owner)
        async with maintenance.startup() as second:
            await c.migrate(owner=second.owner)
        assert second.path is None and second.manifest['status'] == 'no_changes'
        assert not list((c.store.path.parent / 'migration-rollbacks').iterdir())
    finally:
        await c.store.close()


@pytest.mark.asyncio
async def test_explicit_tool_discovery_always_closes_external_service(tmp_path):
    c = await config(tmp_path / 'g', tmp_path / 'p' / 'db')
    catalog = CatalogService(c, None, None)
    calls = []
    class MCP:
        def __init__(self, *args): calls.append('construct')
        async def discover(self):
            calls.append('discover')
            raise RuntimeError('discovery failed')
        async def close(self): calls.append('close')
    try:
        native = await catalog.tool_catalog(discover_external=False, mcp_factory=MCP)
        assert native['tools'] and not calls
        with pytest.raises(RuntimeError):
            await catalog.tool_catalog(mcp_factory=MCP)
        assert calls == ['construct', 'discover', 'close']
    finally:
        await c.store.close()


@pytest.mark.asyncio
async def test_artifact_recovers_identical_orphan_file_without_overwriting(tmp_path):
    store = await artifact_store(tmp_path)
    service = ArtifactService(store)
    service.root.mkdir()
    (service.root / 'stable.json').write_bytes(b'{"result":1}')
    try:
        result = await service.create('a', 'result', identity='stable', suffix='.json', content=b'{"result":1}')
        repeat = await service.create('a', 'result', identity='stable', suffix='.json', content=b'{"result":1}')
        assert result['id'] == repeat['id'] and repeat['complete']
        with pytest.raises(Conflict):
            await service.create('a', 'result', identity='stable', suffix='.json', content=b'changed')
        with pytest.raises(Conflict):
            await service.create('b', 'result', identity='stable', suffix='.json', content=b'{"result":1}')
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_future_settings_are_rejected_before_provider_migration(tmp_path):
    root = tmp_path / 'g'; root.mkdir()
    raw = {'format_version': 99, 'profiles': {'future': {'model': 'model'}}}
    (root / 'settings.json').write_text(json.dumps(raw))
    store = ProjectStore(tmp_path / 'p' / 'db'); await store.open()
    c = Configuration(root, store)
    try:
        with pytest.raises(Exception, match='Unsupported global settings format'):
            await c.migrate()
        assert not (root / 'providers.json').exists()
        assert json.loads((root / 'settings.json').read_text()) == raw
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_legacy_team_edit_compares_normalized_catalog_snapshot(tmp_path):
    c = await config(tmp_path / 'g', tmp_path / 'p' / 'db')
    raw = standard_team().model_dump(mode='json')
    raw.update(id='legacy', format_version=1)
    for member in raw['members']:
        member['allowed_tools'] = ['read']
    path = c.root / 'teams' / 'legacy.json'; path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(raw))
    try:
        team = await c.get_team('global:legacy')
        expected = team.model_dump(mode='json')
        await c.save_team(team.model_copy(update={'name': 'Edited legacy'}), 'global', expected=expected)
        saved = json.loads(path.read_text())
        assert saved['format_version'] == 2 and saved['name'] == 'Edited legacy'
        assert 'read_file' in saved['members'][0]['allowed_tools']
    finally:
        await c.store.close()


@pytest.mark.asyncio
async def test_domain_only_backup_needs_no_project_manager(repository, tmp_path):
    from agilecode.core.application import Application
    app = Application(repository, tmp_path / 'global', execution=False)
    await app.open(initialize=True)
    try:
        assert app.project_manager is None
        result = await app.backup(tmp_path / 'domain.zip')
        assert Path(result['path']).is_file()
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_explicitly_empty_provider_pool_is_not_reinitialized(tmp_path):
    c = await config(tmp_path / 'g', tmp_path / 'p' / 'db')
    try:
        assert not await c.needs_migration()
        c.delete_provider('local')
        assert not await c.needs_migration()
        await c.migrate()
        assert c.providers_snapshot()['providers'] == []
        assert c.provider_pool.default_provider_id is None
    finally:
        await c.store.close()


@pytest.mark.asyncio
async def test_settings_snapshots_hold_one_global_view_through_project_read(tmp_path, monkeypatch):
    a = await config(tmp_path / 'g', tmp_path / 'a' / 'db')
    b = await config(tmp_path / 'g', tmp_path / 'b' / 'db')
    entered, release = asyncio.Event(), asyncio.Event()
    original = a.store.transaction
    async def gated_transaction(fn):
        entered.set()
        await release.wait()
        return await original(fn)
    monkeypatch.setattr(a.store, 'transaction', gated_transaction)
    try:
        await b.patch_settings({'max_corrections': 4}, 'global')
        snapshot_task = asyncio.create_task(a.snapshots())
        await entered.wait()
        with pytest.raises(Conflict):
            await b.patch_settings({'max_corrections': 6}, 'global')
        release.set()
        snapshots = await snapshot_task
        assert snapshots['effective']['max_corrections'] == 4
        assert snapshots['global']['values']['max_corrections'] == 4
        assert snapshots['global']['effective'] == snapshots['project']['effective'] == snapshots['effective']
        await b.patch_settings({'max_corrections': 6}, 'global')
        # The old displayed value cannot silently inherit a newer CAS baseline.
        with pytest.raises(Conflict):
            await a.patch_settings({'max_corrections': 8}, 'global', expected=snapshots['global'])
        assert b.settings.max_corrections == 6
        snapshots['global']['effective']['max_corrections'] = 99
        assert snapshots['effective']['max_corrections'] == 4
    finally:
        release.set()
        await a.store.close(); await b.store.close()


@pytest.mark.asyncio
async def test_repeated_cancel_during_artifact_finalize_retains_then_releases_job(tmp_path, monkeypatch):
    from agilecode.adapters.shell import ShellService, ShellResult
    from agilecode.adapters.workspace import Workspace
    from agilecode.core.types import Events
    store = await artifact_store(tmp_path)
    shell = ShellService(Workspace(tmp_path, tmp_path, tmp_path / 'global'), store, Events(), 'a', None, tmp_path / 'logs')
    entered, release = asyncio.Event(), asyncio.Event()
    original = shell.artifacts.finalize
    async def finalize(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    async def execute(command, **kwargs):
        kwargs['callback'](b'output')
        return ShellResult(0, str(tmp_path), 'output')
    monkeypatch.setattr(shell.artifacts, 'finalize', finalize)
    monkeypatch.setattr(shell.shell, 'execute', execute)
    task = asyncio.create_task(shell.run('true', None))
    try:
        await entered.wait()
        job_id = shell.foreground_id
        task.cancel(); await asyncio.sleep(0)
        task.cancel(); await asyncio.sleep(0)
        assert not task.done() and shell.owns_job(job_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not shell.has_live_jobs() and shell.foreground_id is None
        artifact = await store.one('SELECT * FROM artifacts')
        assert artifact['complete'] == 1
    finally:
        release.set()
        await shell.close()
        await store.close()
