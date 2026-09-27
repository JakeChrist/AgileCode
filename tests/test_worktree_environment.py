import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from agilecode.adapters.environment import WorktreeEnvironment
from agilecode.adapters.shell import ShellService, shell_environment
from agilecode.adapters.store import ProjectStore
from agilecode.adapters.workspace import Workspace
from agilecode.core.types import Events, Settings, AgileCodeError, now
from conftest import git


@pytest.fixture
async def shells(tmp_path):
    store = ProjectStore(tmp_path / 'environment.db')
    await store.open()
    await store.insert('tickets', {'id': 'ticket', 'key': 'T-001', 'title': 'Environment', 'description': '',
        'state': 'In Progress', 'team_ref': 'builtin:standard', 'process_id': 'development', 'created': now(), 'updated': now()})
    made = []
    def create(work):
        work.mkdir(exist_ok=True)
        shell = ShellService(Workspace(work, work, tmp_path/'global'), store, Events(),
                             'ticket', None, tmp_path/'artifacts')
        made.append(shell)
        return shell
    yield create, store
    for shell in made:
        await shell.close()
    await store.close()


def settings(**kwargs):
    return Settings(python_interpreter=sys._base_executable, **kwargs)


def test_host_python_environment_is_not_inherited(monkeypatch):
    monkeypatch.setenv('VIRTUAL_ENV', '/host/environment')
    monkeypatch.setenv('PATH', '/host/environment/bin:/usr/bin:/bin')
    monkeypatch.setenv('PYTHONPATH', '/host/source')
    monkeypatch.setenv('PIP_TARGET', '/host/packages')
    env = shell_environment(isolate_python=True)
    assert shell_environment()['VIRTUAL_ENV'] == '/host/environment'  # MCP owns its environment.
    assert env['PATH'] == '/usr/bin:/bin'
    assert not {'VIRTUAL_ENV', 'PYTHONPATH', 'PIP_TARGET'} & env.keys()


async def test_worktrees_have_separate_reusable_venvs_and_background_inherits(shells, tmp_path):
    create, store = shells
    for name in ('one', 'two'):
        shell = create(tmp_path/name)
        (shell.workspace.root/'requirements.txt').write_text('')
        info = await WorktreeEnvironment(shell, settings()).prepare()
        assert info['managed'] and info['status'] == 'ready'
        venv = shell.workspace.root/'.venv'
        marker = venv/'.agilecode-environment.json'
        before = marker.read_text()
        observed = await shell.run("python -c 'import sys; print(sys.prefix)'", None)
        assert observed['output'].strip() == str(venv)
        await shell.run("python -c 'import sys; print(sys.prefix)' > background.txt", None, background=True)
        for proc, task in shell.background.values():
            await task
        assert (shell.workspace.root/'background.txt').read_text().strip() == str(venv)
        await shell.close()
        shell = create(shell.workspace.root)  # Reopening the app constructs a fresh shell.
        await WorktreeEnvironment(shell, settings()).prepare()
        installs = await store.query("SELECT * FROM jobs WHERE cwd=? AND command='python -m pip install -r requirements.txt'", (str(shell.workspace.root),))
        assert len(installs) == 1
        assert marker.read_text() == before
    assert (tmp_path/'one/.venv').resolve() != (tmp_path/'two/.venv').resolve()


async def test_declared_setup_and_manifest_changes(shells, tmp_path):
    create, store = shells
    shell = create(tmp_path/'work')
    cfg = settings(python_environment='venv', setup_commands=["python -c 'import sys; print(sys.prefix)' > setup.txt"])
    await WorktreeEnvironment(shell, cfg).prepare()
    assert (shell.workspace.root/'setup.txt').read_text().strip() == str(shell.workspace.root/'.venv')
    # This path is ignored even with Python versions that do not create .gitignore.
    git(shell.workspace.root, 'init', '-b', 'master')
    assert git(shell.workspace.root, 'check-ignore', '.venv/pyvenv.cfg') == '.venv/pyvenv.cfg'
    await shell.close()
    (shell.workspace.root/'requirements.txt').write_text('# changed\n')
    await WorktreeEnvironment(shell, settings()).prepare()
    assert await store.one("SELECT id FROM jobs WHERE command='python -m pip install -r requirements.txt'")


async def test_shared_symlink_rejected_and_non_python_skipped(shells, tmp_path):
    create, _ = shells
    shell = create(tmp_path/'work')
    assert not (await WorktreeEnvironment(shell, settings()).prepare())['managed']
    shared = tmp_path/'shared'; shared.mkdir()
    (shell.workspace.root/'.venv').symlink_to(shared, target_is_directory=True)
    with pytest.raises(AgileCodeError, match='symlink'):
        await WorktreeEnvironment(shell, settings(python_environment='venv')).prepare()
