"""The inspector reports owned activity and retained feedback, including history."""
import asyncio
import pytest
from agilecode.core.application import Application
from agilecode.core.integration import MergeCoordinator
from agilecode.runtime import conversation_records as records
from conftest import AnalysisClient, ScriptClient
from test_git_recovery import setup_run


@pytest.mark.asyncio
async def test_detail_can_pause_completed_run_with_owned_integration(repository, tmp_path, monkeypatch):
    entered = asyncio.Event()
    async def preparing(self, *args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(MergeCoordinator, '_prepare', preparing)
    app, ticket, run = await setup_run(repository, tmp_path/'global')
    try:
        await app.wait_for_run(run['id'])
        await asyncio.wait_for(entered.wait(), 10)
        detail = await app.ticket_detail(ticket['id'])
        assert detail['run']['state'] == 'completed'
        assert detail['execution']['can_pause']
        assert detail['execution']['active_activities'] == ['integration']
        assert not detail['execution']['can_resume']
        await app.pause_run(ticket['id'])
        detail = await app.ticket_detail(ticket['id'])
        assert not detail['execution']['can_pause']
        assert detail['execution']['active_activities'] == []
        assert detail['run']['final_oid'] == (await app.selected_run(ticket['id']))['final_oid']
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_late_feedback_detail_keeps_exact_text_and_disposition_on_reopen(repository, tmp_path):
    app = Application(repository, tmp_path/'global', client=AnalysisClient())
    await app.open(initialize=True)
    from agilecode.core.types import Settings
    await app.config.save(Settings(autonomy="full", merge_on_completion=True))
    ticket = await app.create_ticket('Analyze feedback', 'Keep later corrections visible.',
        team_ref='builtin:requirements-engineering', process_id='requirements-definition')
    await app.move_ticket(ticket['id'], 'Ready')
    run = await app.start_ticket(ticket['id'])
    await app.wait_for_run(run['id'])
    await app.prepare_integration(ticket['id'])
    attempt = await app.store.one('SELECT * FROM attempts WHERE run_id=? ORDER BY rowid DESC LIMIT 1', (run['id'],))
    text = 'Keep the second finding.\nUse the exact project term: α.'
    receipt = await records.accept_ticket_feedback(app.store, ticket['id'], run['id'], attempt['session_id'], text, 'late-input')
    duplicate = await records.accept_ticket_feedback(app.store, ticket['id'], run['id'], attempt['session_id'], text, 'late-input')
    assert receipt['id'] == duplicate['id']
    original = None
    try:
        for _ in range(2):
            detail = await app.ticket_detail(ticket['id'])
            feedback = detail['pending_feedback']
            assert len(feedback) == 1
            assert feedback[0]['text'] == text
            assert feedback[0]['receipt_id'] == receipt['id']
            assert feedback[0]['input_id'] == 'late-input'
            assert feedback[0]['run_id'] == run['id']
            assert feedback[0]['disposition']['status'] == 'needs_followup'
            assert feedback[0]['disposition'] == receipt['outcome']
            assert detail['ticket']['state'] == 'Done'
            if original is not None:
                assert feedback == original
            original = feedback
    finally:
        await app.close()
    reopened = Application(repository, tmp_path/'global', execution=False)
    await reopened.open()
    try:
        assert (await reopened.ticket_detail(ticket['id']))['pending_feedback'] == original
        assert len(await reopened.store.query('SELECT * FROM runs WHERE ticket_id=?', (ticket['id'],))) == 1
    finally:
        await reopened.close()
