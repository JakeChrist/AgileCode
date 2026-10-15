"""Pause must stop live work, not just prevent the next step."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from agilecode.adapters.model import Turn
from agilecode.core.application import Application
from agilecode.core.types import Settings
from conftest import ScriptClient
from test_git_recovery import setup_run


async def until(predicate):
    async with asyncio.timeout(5):
        while not await predicate():
            await asyncio.sleep(.01)


class PausableClient(ScriptClient):
    def __init__(self, command=None, background=False):
        super().__init__()
        self.started = asyncio.Event()
        self.command, self.background = command, background
        self.first = True
        self.delta = None
        self.requests = 0

    async def stream(self, profile, messages, tools, on_delta):
        self.requests += 1
        if self.first and (not self.command or 'implement' in messages[0]['content'].split('CURRENT STEP:')[1].split('\n')[0]):
            self.first = False
            self.delta = on_delta
            self.started.set()
            if self.command:
                return Turn({'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'pause-command', 'type': 'function', 'function': {'name': 'run_command',
                     'arguments': json.dumps({'command': self.command, 'background': self.background})}}]}, 'fixture-model', None, 'tool_calls')
            on_delta('text', 'Before pause')
            await asyncio.Event().wait()
        # Consume an interrupted command result without replaying the command.
        messages = [m for m in messages if m.get('tool_call_id') != 'pause-command'
                    and not any(c['id'] == 'pause-command' for c in m.get('tool_calls', []))]
        return await super().stream(profile, messages, tools, on_delta)


async def test_pause_cancels_stream_suppresses_late_deltas_and_resumes(repository, tmp_path):
    client = PausableClient()
    app, ticket, run = await setup_run(repository, tmp_path/'global', client)
    events = []
    unsubscribe = app.events.subscribe(events.append)
    try:
        await asyncio.wait_for(client.started.wait(), 3)
        await asyncio.wait_for(app.pause_run(ticket['id']), 3)
        assert (await app.store.get('runs', run['id']))['state'] == 'paused'
        assert app.tasks[run['id']].done()
        before = len(events)
        client.delta('text', 'Late unwanted response')
        await asyncio.sleep(.05)
        assert len(events) == before
        assert client.requests == 1
        rows = await app.store.query("SELECT * FROM messages WHERE partial=1")
        assert len(rows) == 1 and 'Before pause' in rows[0]['body']
        await app.resume_run(ticket['id'])
        assert (await asyncio.wait_for(app.wait_for_run(run['id']), 10))['state'] == 'completed'
    finally:
        await app.close()


@pytest.mark.parametrize('background', [False, True])
async def test_pause_stops_foreground_and_background_without_replay(repository, tmp_path, background):
    command = "echo started >> executions.txt; echo $$ > command.pid; sleep 60"
    client = PausableClient(command, background)
    app, ticket, run = await setup_run(repository, tmp_path/'global', client)
    try:
        async def running():
            return await app.store.one("SELECT id FROM jobs WHERE command=? AND status='running'", (command,))
        await until(running)
        await asyncio.sleep(.05)
        runtime = app.runtimes[run['id']]
        pids = [proc.pid for proc, _ in runtime.shell.background.values()]
        if runtime.shell.shell.process:
            pids.append(runtime.shell.shell.process.pid)
        await asyncio.wait_for(app.pause_run(ticket['id']), 4)
        assert not runtime.shell.has_live_jobs()
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert not await app.store.query("SELECT * FROM jobs WHERE status IN ('running','starting')")
        assert (Path(run['workspace'])/'executions.txt').read_text().count('started') == 1
        if not background:
            await app.resume_run(ticket['id'])
            assert (await asyncio.wait_for(app.wait_for_run(run['id']), 10))['state'] == 'completed'
            assert (Path(run['workspace'])/'executions.txt').read_text().count('started') == 1
    finally:
        await app.close()


async def test_answer_does_not_restart_explicitly_paused_ticket(repository, tmp_path):
    client = ScriptClient(question=True)
    app, ticket, run = await setup_run(repository, tmp_path/'global', client)
    try:
        async def waiting():
            return await app.store.one("SELECT * FROM waits WHERE status='pending'")
        await until(waiting)
        wait = await waiting()
        await app.pause_run(ticket['id'])
        calls = client.calls
        await app.answer(wait['id'], 'Yes')
        await asyncio.sleep(.05)
        assert client.calls == calls
        assert (await app.store.get('runs', run['id']))['state'] == 'paused'
        await app.resume_run(ticket['id'])
        assert (await asyncio.wait_for(app.wait_for_run(run['id']), 10))['state'] == 'completed'
    finally:
        await app.close()


async def test_pause_during_setup_stops_process_before_any_model_request(repository, tmp_path):
    client = ScriptClient()
    app = Application(repository, tmp_path/'global', client=client)
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy='full', setup_commands=['echo setup > setup.log; sleep 60']))
    ticket = await app.create_ticket('Setup pause', 'Make answer return two.')
    await app.move_ticket(ticket['id'], 'Ready')
    run = await app.start_ticket(ticket['id'])
    try:
        async def running():
            return await app.store.one("SELECT * FROM jobs WHERE status='running'")
        await until(running)
        await app.pause_run(ticket['id'])
        assert client.calls == 0
        assert not app.runtimes[run['id']].shell.has_live_jobs()
        assert not await app.store.query("SELECT * FROM jobs WHERE status IN ('running','starting')")
        await app.config.save(Settings(autonomy='full', setup_commands=['echo resumed > setup.log']))
        await app.resume_run(ticket['id'])
        assert (await asyncio.wait_for(app.wait_for_run(run['id']), 10))['state'] == 'completed'
        assert (Path(run['workspace'])/'setup.log').read_text().strip() == 'resumed'
    finally:
        await app.close()


async def test_pause_cancels_context_summary_request(repository, tmp_path):
    class SummaryClient(ScriptClient):
        def __init__(self):
            super().__init__()
            self.started, self.cancelled = asyncio.Event(), asyncio.Event()
        async def stream(self, profile, messages, tools, on_delta):
            if messages[0].get("content", "").startswith("Compress the supplied prior context"):
                assert on_delta is not None  # Summary requests now use tracked generation.
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()
            return await super().stream(profile, messages, tools, on_delta)
    client = SummaryClient()
    app = Application(repository, tmp_path/'global', client=client)
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy='full'))
    async def compact(messages, window, summarize):
        await summarize('Prior context')
        return messages, {}
    app.processor.agent.context_manager.prepare = compact
    ticket = await app.create_ticket('Summary pause', 'Make answer return two.')
    await app.move_ticket(ticket['id'], 'Ready')
    run = await app.start_ticket(ticket['id'])
    try:
        await asyncio.wait_for(client.started.wait(), 3)
        await app.pause_run(ticket['id'])
        assert client.cancelled.is_set()
        assert app.tasks[run['id']].done()
        assert client.calls == 0
    finally:
        await app.close()


async def test_pausing_one_ticket_does_not_stop_another(repository, tmp_path):
    client = PausableClient()
    app, first, run = await setup_run(repository, tmp_path/'global', client)
    try:
        await asyncio.wait_for(client.started.wait(), 3)
        second = await app.create_ticket('Second independent ticket', 'Make answer return two.')
        await app.move_ticket(second['id'], 'Ready')
        other = await app.start_ticket(second['id'])
        await app.pause_run(first['id'])
        assert (await asyncio.wait_for(app.wait_for_run(other['id']), 10))['state'] == 'completed'
        assert (await app.store.get('runs', run['id']))['state'] == 'paused'
    finally:
        await app.close()


async def test_managed_environment_survives_app_restart_and_is_cleaned_with_worktree(repository, tmp_path):
    import sys
    from conftest import git
    (repository/'requirements.txt').write_text('')
    git(repository, 'add', 'requirements.txt')
    git(repository, 'commit', '-m', 'Declare Python dependencies')
    client = PausableClient()
    app = Application(repository, tmp_path/'global', client=client)
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy='full', python_interpreter=sys._base_executable))
    ticket = await app.create_ticket('Managed environment', 'Make answer return two.')
    await app.move_ticket(ticket['id'], 'Ready')
    run = await app.start_ticket(ticket['id'])
    work = Path(run['workspace'])
    try:
        await asyncio.wait_for(client.started.wait(), 15)
        marker = work/'.venv/.agilecode-environment.json'
        saved = marker.read_text()
        request = await app.store.one('SELECT input FROM requests ORDER BY created LIMIT 1')
        assert str(work/'.venv/bin/python') in request['input']
        await app.pause_run(ticket['id'])
        assert marker.read_text() == saved
    finally:
        await app.close()
    reopened = Application(repository, tmp_path/'global', client=ScriptClient())
    await reopened.open()
    try:
        await reopened.resume_run(ticket['id'])
        assert (await asyncio.wait_for(reopened.wait_for_run(run['id']), 15))['state'] == 'completed'
        assert marker.read_text() == saved
        installs = await reopened.store.query("SELECT id FROM jobs WHERE command='python -m pip install -r requirements.txt' AND run_id=?", (run['id'],))
        assert len(installs) == 1
        candidate = await reopened.prepare_integration(ticket['id'])
        await reopened.approve_integration(candidate['id'], candidate['candidate_oid'], candidate['target_oid'])
        assert not work.exists()
        assert '.venv' not in git(repository, 'ls-files')
    finally:
        await reopened.close()


async def test_failed_setup_is_retried_before_model_can_resume(repository, tmp_path):
    app = Application(repository, tmp_path/'global', client=ScriptClient())
    await app.open(initialize=True)
    await app.config.save(Settings(autonomy='full', setup_commands=['cd tests; false']))
    ticket = await app.create_ticket('Retry setup', 'Make answer return two.')
    await app.move_ticket(ticket['id'], 'Ready')
    run = await app.start_ticket(ticket['id'])
    try:
        assert (await app.wait_for_run(run['id']))['state'] == 'paused'
        assert not await app.store.query('SELECT * FROM requests')
        await app.config.save(Settings(autonomy='full', setup_commands=['pwd > setup-root.txt']))
        await app.resume_run(ticket['id'])
        assert (await asyncio.wait_for(app.wait_for_run(run['id']), 10))['state'] == 'completed'
        assert (Path(run['workspace'])/'setup-root.txt').read_text().strip() == run['workspace']
    finally:
        await app.close()
