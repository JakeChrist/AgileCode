"""A rejected command is not latent permission for recovery to execute it."""
import asyncio
import pytest
from agilecode.core.application import Application
from agilecode.core.types import AgileCodeError, Conflict


@pytest.mark.asyncio
async def test_capacity_rejection_stays_rejected_after_capacity_frees_and_reopen(repository, tmp_path):
    app = Application(repository, tmp_path/'global', execution=False)
    await app.open(initialize=True)
    first = await app.create_ticket('First', 'First')
    rejected = await app.create_ticket('Rejected', 'Rejected')
    scope = {'scope':'project_manager', 'admission_limit':1}
    args = dict(request_id='rejected-capacity', caller=scope, activate=False)
    try:
        await app.move_ticket(first['id'], 'In Progress', caller=scope, activate=False)
        with pytest.raises(AgileCodeError, match='Maximum concurrent'):
            await app.move_ticket(rejected['id'], 'In Progress', **args)
        receipt = await app.store.get('operations', 'rejected-capacity')
        assert receipt['status'] == 'rejected'
        await app.move_ticket(first['id'], 'Ready')
        await app.execution_ops.recover_pending_operations()
        with pytest.raises(AgileCodeError, match='Maximum concurrent'):
            await app.move_ticket(rejected['id'], 'In Progress', **args)
        assert (await app.find_ticket(rejected['id']))['state'] == 'Backlog'
        assert await app.store.get('operations', 'rejected-capacity') == receipt
    finally:
        await app.close()
    for _ in range(2):
        reopened = Application(repository, tmp_path/'global', execution=False)
        await reopened.open()
        try:
            await reopened.execution_ops.recover_pending_operations()
            with pytest.raises(AgileCodeError, match='Maximum concurrent'):
                await reopened.move_ticket(rejected['id'], 'In Progress', **args)
            assert (await reopened.find_ticket(rejected['id']))['state'] == 'Backlog'
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_cas_rejection_is_terminal_and_competitor_preserves_owner(repository, tmp_path):
    app = Application(repository, tmp_path/'global', execution=False)
    await app.open(initialize=True)
    release = asyncio.Event()
    try:
        ticket = await app.create_ticket('Before', 'Description')
        ticket = await app.move_ticket(ticket['id'], 'In Progress', activate=False)
        entered = asyncio.Event()
        async def stop(_):
            entered.set()
            await release.wait()
        app.execution_ops.stop_ticket = stop
        args = dict(expected_revision=ticket['revision'], request_id='cas-owner')
        moving = asyncio.create_task(app.move_ticket(ticket['id'], 'Ready', **args))
        await entered.wait()
        lease = app.coordinator.reservations[ticket['id']]
        # Same identity racing itself cannot reject the first admitted command.
        with pytest.raises(Conflict, match='busy'):
            await app.move_ticket(ticket['id'], 'Ready', **args)
        assert (await app.store.get('operations', 'cas-owner'))['status'] == 'reserved'
        with pytest.raises(Conflict, match='busy'):
            await app.move_ticket(ticket['id'], 'Done', request_id='competitor')
        assert app.coordinator.reservations[ticket['id']] is lease
        edited = await app.edit_ticket(ticket['id'], expected_revision=ticket['revision'], title='After')
        release.set()
        with pytest.raises(Conflict, match='changed while'):
            await moving
        assert (await app.store.get('operations', 'cas-owner'))['status'] == 'rejected'
        assert (await app.store.get('operations', 'competitor'))['status'] == 'rejected'
        for _ in range(2):
            await app.execution_ops.recover_pending_operations()
            with pytest.raises(Conflict, match='changed while'):
                await app.move_ticket(ticket['id'], 'Ready', **args)
            with pytest.raises(Conflict, match='busy'):
                await app.move_ticket(ticket['id'], 'Done', request_id='competitor')
        current = await app.find_ticket(ticket['id'])
        assert current['state'] == 'In Progress'
        assert current['revision'] == edited['revision']
    finally:
        release.set()
        await app.close()


@pytest.mark.asyncio
async def test_uncertain_stop_retains_visible_blocker_after_reopen(repository, tmp_path):
    app = Application(repository, tmp_path/'global', execution=False)
    await app.open(initialize=True)
    ticket = await app.create_ticket('Unknown stop', 'Description')
    await app.move_ticket(ticket['id'], 'In Progress', activate=False)
    async def uncertain(_):
        raise AgileCodeError('Cannot establish physical job termination.')
    app.execution_ops.stop_ticket = uncertain
    try:
        with pytest.raises(AgileCodeError, match='physical job'):
            await app.move_ticket(ticket['id'], 'Ready', request_id='unknown-stop')
        assert (await app.store.get('operations', 'unknown-stop'))['status'] == 'blocked'
        assert (await app.ticket_execution(ticket['id']))['blocking_reason']
        with pytest.raises(Conflict):
            await app.move_ticket(ticket['id'], 'Done')
    finally:
        await app.close()
    reopened = Application(repository, tmp_path/'global', execution=False)
    await reopened.open()
    try:
        assert (await reopened.find_ticket(ticket['id']))['state'] == 'In Progress'
        assert ticket['id'] in reopened.coordinator.blocked_tickets
        with pytest.raises(Conflict):
            await reopened.move_ticket(ticket['id'], 'Ready', request_id='unknown-stop')
    finally:
        await reopened.close()
