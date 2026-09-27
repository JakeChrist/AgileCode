"""Provision a Python environment owned by one worktree, before agent execution."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path

from agilecode.core.types import AgileCodeError, Settings
from .shell import ShellService, shell_environment


class WorktreeEnvironment:
    MANIFESTS = ('pyproject.toml', 'requirements.txt', 'setup.py', 'setup.cfg',
                 'uv.lock', 'poetry.lock', 'Pipfile', 'Pipfile.lock')

    def __init__(self, shell: ShellService, settings: Settings):
        self.shell, self.settings = shell, settings
        self.root = shell.workspace.root
        self.venv = self.root / '.venv'

    async def command(self, command: str):
        result = await self.shell.run(command, None, timeout=self.settings.shell_command_timeout_seconds)
        if result['exit_code'] != 0:
            raise AgileCodeError(f'Worktree environment setup failed: {command}\n{result["output"][-4000:]}')
        return result

    async def prepare(self) -> dict:
        # A failed setup can leave cwd and exports behind. Retrying always starts
        # at the worktree root with the same clean environment as a new ticket.
        await self.shell.shell.close()
        self.shell.shell.environment = shell_environment(isolate_python=True)
        mode = self.settings.python_environment
        detected = self.venv.exists() or any((self.root / name).is_file() for name in self.MANIFESTS)
        enabled = mode == 'venv' or (mode == 'auto' and detected)
        if not enabled:
            await self.shell.shell.start()
            for command in self.settings.setup_commands:
                await self.command(command)
            return {'managed': False, 'status': 'disabled' if mode == 'disabled' else 'not a Python project'}
        if self.venv.is_symlink():
            raise AgileCodeError('The worktree .venv must be a local directory, not a shared environment symlink.')
        creating = self.venv / '.agilecode-creating'
        if (self.venv / 'pyvenv.cfg').exists() and not creating.exists():
            config = (self.venv / 'pyvenv.cfg').read_text()
            if 'include-system-site-packages = true' in config:
                raise AgileCodeError('The worktree .venv includes system packages; recreate it without system-site-packages.')
        else:
            # Use a subprocess owned by ShellService so Pause also stops provisioning.
            self.venv.mkdir(exist_ok=True)
            creating.touch()
            await self.command(f'{shlex.quote(self.settings.python_interpreter)} -m venv .venv')
            creating.unlink()
        python = self.venv / 'bin' / 'python'
        if not python.exists():
            raise AgileCodeError('The worktree .venv is incomplete. Repair it before resuming.')

        # This is applied on every new shell, even when dependencies are already ready.
        env = self.shell.shell.environment
        env['VIRTUAL_ENV'] = str(self.venv)
        env['PATH'] = os.pathsep.join([str(self.venv / 'bin')] + [p for p in env['PATH'].split(os.pathsep) if p != str(self.venv / 'bin')])
        env['PIP_REQUIRE_VIRTUALENV'] = 'true'
        await self.shell.shell.close()
        await self.shell.shell.start()
        (self.venv / '.gitignore').write_text('*\n')
        marker = self.venv / '.agilecode-environment.json'
        manifests = {name: (self.root / name).read_text() for name in self.MANIFESTS if (self.root / name).is_file()}
        signature = hashlib.sha256(json.dumps({
            'manifests': manifests, 'interpreter': self.settings.python_interpreter,
            'setup': self.settings.setup_commands,
        }, sort_keys=True).encode()).hexdigest()
        try:
            saved = json.loads(marker.read_text())
        except (OSError, ValueError):
            saved = {}
        if saved.get('signature') != signature and not self.settings.setup_commands:
            if 'uv.lock' in manifests:
                await self.command('uv sync --frozen --active')
            elif any(name in manifests for name in ('poetry.lock', 'Pipfile.lock', 'Pipfile')):
                raise AgileCodeError('This project uses a dependency manager. Configure its install command in Settings → Worktree setup commands; it must use this worktree\'s .venv.')
            elif 'requirements.txt' in manifests:
                await self.command('python -m pip install -r requirements.txt')
            elif 'pyproject.toml' in manifests or 'setup.py' in manifests or 'setup.cfg' in manifests:
                await self.command('python -m pip install -e .')
        # User-declared commands may establish shell state, so run on each shell
        # startup. Install commands should be repeatable (pip/uv already are).
        for command in self.settings.setup_commands:
            await self.command(command)
        checked = await self.command("python -c 'import sys; print(sys.prefix)' ")
        if checked['output'].strip() != str(self.venv):
            raise AgileCodeError('Setup selected a Python environment outside this worktree. Update the setup commands to use .venv.')
        marker.write_text(json.dumps({'signature': signature}) + '\n')
        return {'managed': True, 'status': 'ready', 'path': str(self.venv), 'python': str(python),
                'instruction': 'python and python -m pip already use this worktree .venv. Do not activate an environment from the original checkout.'}
