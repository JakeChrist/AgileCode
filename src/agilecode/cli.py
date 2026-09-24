"""A small headless adapter. It invokes exactly the same services as the desktop UI."""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from .core.application import Application
from .core.types import AgileCodeError, Settings, Team
from .adapters.store import decode


def parser():
    p=argparse.ArgumentParser(prog="agilecode",description="Headless ticket-based coding agents; run `agilecode gui` for the desktop interface.")
    p.add_argument("-p","--project",default=".",help="Original Git project root")
    p.add_argument("--global-dir",default=None,help="Override ~/.agilecode (useful for isolated tests)")
    p.add_argument("--json",action="store_true",help="Print structured JSON instead of the board table")
    sub=p.add_subparsers(dest="command",required=True)
    q=sub.add_parser("init",help="Initialize project data and add .agilecode/ to .gitignore");q.add_argument("path",nargs="?")
    sub.add_parser("gui",help="Launch the replaceable QML desktop frontend")
    q=sub.add_parser("board");q.add_argument("--filter",default="");q.add_argument("--archived",action="store_true")
    q=sub.add_parser("create");q.add_argument("title");q.add_argument("--description",default="");q.add_argument("--description-file");q.add_argument("--team");q.add_argument("--process");q.add_argument("--parent");q.add_argument("--ready",action="store_true")
    q=sub.add_parser("show");q.add_argument("ticket")
    q=sub.add_parser("move");q.add_argument("ticket");q.add_argument("state",choices=["Backlog","Ready","In Progress","Done"])
    for name in ("run","resume"):
        q=sub.add_parser(name,help="Run and keep the engine alive, handling questions/approvals interactively")
        q.add_argument("ticket");q.add_argument("--non-interactive",action="store_true");q.add_argument("--allow-dirty-base",action="store_true")
    for name in ("pause","cancel","cleanup","archive","reopen"):
        q=sub.add_parser(name);q.add_argument("ticket")
    q=sub.add_parser("feedback");q.add_argument("ticket");q.add_argument("text")
    q=sub.add_parser("answer");q.add_argument("wait_id");q.add_argument("text");q.add_argument("--non-interactive",action="store_true")
    q=sub.add_parser("conversation");q.add_argument("session");q.add_argument("--after",type=int,default=0)
    q=sub.add_parser("diff");q.add_argument("ticket")
    q=sub.add_parser("checkpoint");q.add_argument("ticket");q.add_argument("--label",default="User checkpoint")
    q=sub.add_parser("restore-checkpoint");q.add_argument("ticket");q.add_argument("checkpoint");q.add_argument("--expected-tree",required=True)
    q=sub.add_parser("preview-checkpoint");q.add_argument("ticket");q.add_argument("checkpoint")
    q=sub.add_parser("prepare-merge");q.add_argument("ticket");q.add_argument("--rebuild",action="store_true");q.add_argument("--non-interactive",action="store_true")
    q=sub.add_parser("merge");q.add_argument("integration");q.add_argument("--candidate",required=True);q.add_argument("--target",required=True)
    q=sub.add_parser("settings");q.add_argument("action",choices=["show","save","reset-project"]);q.add_argument("file",nargs="?");q.add_argument("--scope",choices=["global","project"],default="project")
    q=sub.add_parser("teams");q.add_argument("action",choices=["list","export","save"],default="list",nargs="?");q.add_argument("value",nargs="?");q.add_argument("--scope",choices=["global","project"],default="global")
    sub.add_parser("models");sub.add_parser("skills");sub.add_parser("recovery")
    q=sub.add_parser("reconcile");q.add_argument("kind",choices=["tool","job"]);q.add_argument("id");q.add_argument("explanation");q.add_argument("--not-executed",action="store_true")
    q=sub.add_parser("job");q.add_argument("id");q.add_argument("--offset",type=int,default=0)
    q=sub.add_parser("backup");q.add_argument("destination")
    q=sub.add_parser("restore-backup");q.add_argument("source");q.add_argument("destination")
    q=sub.add_parser("export-ticket");q.add_argument("ticket");q.add_argument("destination")
    q=sub.add_parser("discussions");q.add_argument("--archived",action="store_true")
    q=sub.add_parser("new-discussion");q.add_argument("--title")
    q=sub.add_parser("discuss");q.add_argument("question");q.add_argument("--discussion")
    return p


async def interact(app, operation, non_interactive=False):
    """Keep one engine owner alive while servicing durable human interaction."""
    task=asyncio.ensure_future(operation)
    prompted=set()
    try:
        while not task.done():
            for wait in await app.store.query("SELECT * FROM waits WHERE status='pending' ORDER BY created"):
                if wait["id"] in prompted:continue
                if non_interactive:
                    # Closing the host leaves the durable wait for `answer`/GUI resume.
                    task.cancel(); await asyncio.gather(task,return_exceptions=True)
                    return {"status":"waiting","wait":wait}
                prompted.add(wait["id"])
                print("\n"+wait["prompt"],file=sys.stderr)
                prompt="Approve this exact action? [y/N] " if wait["kind"]=="approval" else "Answer: "
                answer=await asyncio.to_thread(input,prompt)
                if wait["kind"]=="approval":answer="approve" if answer.lower() in {"y","yes"} else "deny"
                await app.answer(wait["id"],answer)
            await asyncio.sleep(.08)
        return await task
    finally:
        if not task.done():task.cancel()


async def execute(args):
    if args.command=="restore-backup":
        from .core.backup import restore_backup
        return await restore_backup(Path(args.source),Path(args.destination),Path(args.global_dir) if args.global_dir else None)
    app=Application(args.path if args.command=="init" and args.path else args.project,args.global_dir)
    unsub=None
    try:
        opened=await app.open(initialize=args.command=="init")
        if args.command=="init":return opened
        if args.command in {"run","resume","answer","prepare-merge"}:
            def progress(event):
                if event.kind in {"StepAttemptStarted","StepAttemptCompleted","IntegrationReady","IntegrationBlocked","ExecutionCompleted","RecoveryRequired"}:
                    print(f"{event.kind}: "+json.dumps(event.data,ensure_ascii=False),file=sys.stderr)
            unsub=app.events.subscribe(progress)
        command=args.command
        if command=="board":return await app.board(args.filter,include_archived=args.archived)
        if command=="create":
            description=Path(args.description_file).read_text() if args.description_file else args.description
            parent=(await app.find_ticket(args.parent))["id"] if args.parent else None
            ticket=await app.create_ticket(args.title,description,team_ref=args.team,process_id=args.process,parent_id=parent)
            if args.ready:ticket=await app.move_ticket(ticket["id"],"Ready")
            return ticket
        if command=="show":return await app.ticket_detail(args.ticket)
        if command=="move":
            if args.state=="In Progress":raise AgileCodeError("Use `run T-001` to start a ticket and keep the CLI engine alive.")
            return await app.move_ticket(args.ticket,args.state)
        if command in {"run","resume"}:
            async def run():
                if command=="run":record=await app.start_ticket(args.ticket,allow_dirty_base=args.allow_dirty_base)
                else:
                    await app.resume_run(args.ticket)
                    ticket=await app.find_ticket(args.ticket);record=await app.latest_run(ticket["id"])
                result=await app.wait_for_run(record["id"])
                ticket=await app.find_ticket(args.ticket)
                merge_task=app.integration_tasks.get(ticket["id"])
                if merge_task:await merge_task
                return await app.ticket_detail(args.ticket)
            return await interact(app,run(),args.non_interactive)
        if command=="pause":await app.pause_run(args.ticket);return {"paused":args.ticket}
        if command=="cancel":await app.cancel_run(args.ticket);return {"cancelled":args.ticket}
        if command=="cleanup":return await app.retry_cleanup(args.ticket)
        if command=="archive":await app.archive_ticket(args.ticket);return {"archived":args.ticket}
        if command=="reopen":return await app.move_ticket(args.ticket,"Backlog")
        if command=="feedback":await app.submit_feedback(args.ticket,args.text);return {"queued":True}
        if command=="answer":
            wait=await app.store.get("waits",args.wait_id)
            async def answer():
                await app.answer(args.wait_id,args.text)
                run=await app.latest_run(wait["ticket_id"])
                if run:await app.wait_for_run(run["id"])
                merge_task=app.integration_tasks.get(wait["ticket_id"])
                if merge_task:await merge_task
                return await app.ticket_detail(wait["ticket_id"])
            return await interact(app,answer(),args.non_interactive)
        if command=="conversation":return await app.conversation(args.session,after=args.after)
        if command=="diff":return await app.diff(args.ticket)
        if command=="checkpoint":return await app.checkpoint(args.ticket,args.label)
        if command=="preview-checkpoint":
            ticket=await app.find_ticket(args.ticket);run=await app.latest_run(ticket["id"])
            return await app.checkpoints.preview(run,args.checkpoint)
        if command=="restore-checkpoint":return await app.restore_checkpoint(args.ticket,args.checkpoint,args.expected_tree)
        if command=="prepare-merge":return await interact(app,app.prepare_integration(args.ticket,rebuild=args.rebuild),args.non_interactive)
        if command=="merge":return await app.approve_integration(args.integration,args.candidate,args.target)
        if command=="settings":
            if args.action=="show":return app.config.settings.model_dump(mode="json")
            if args.action=="reset-project":
                await app.store.execute("DELETE FROM settings WHERE key='project'");await app.config.load();return {"inheriting":"global settings"}
            if not args.file:raise AgileCodeError("Provide a JSON settings file.")
            settings=Settings.model_validate_json(Path(args.file).read_text())
            await app.config.save(settings,args.scope);return {"saved":args.scope}
        if command=="teams":
            if args.action=="list":return await app.config.list_teams()
            if not args.value:raise AgileCodeError("Provide a team reference to export or a JSON file to save.")
            if args.action=="export":return (await app.config.get_team(args.value)).model_dump(mode="json")
            return {"saved":await app.config.save_team(Team.model_validate_json(Path(args.value).read_text()),args.scope)}
        if command=="models":return await app.client.models(app.config.settings.profiles[app.config.settings.active_profile])
        if command=="skills":return await asyncio.to_thread(app.skills.catalog,["*"])
        if command=="recovery":return await app.recovery_items()
        if command=="reconcile":await app.resolve_unknown(args.id,args.kind,args.explanation,not_executed=args.not_executed);return {"reconciled":args.id}
        if command=="job":return await app.job_output(args.id,args.offset)
        if command=="backup":return await app.backup(args.destination)
        if command=="export-ticket":return await app.export_ticket(args.ticket,args.destination)
        if command=="discussions":return await app.discussion.list(include_archived=args.archived)
        if command=="new-discussion":return await app.discussion.create(args.title)
        if command=="discuss":
            discussion=await app.discussion.get(args.discussion) if args.discussion else await app.discussion.create()
            return await app.discussion.ask(discussion["id"],args.question)
        raise AgileCodeError("Unknown command")
    finally:
        if unsub:unsub()
        await app.close()


def main():
    args=parser().parse_args()
    if args.command=="gui":
        try:
            from .desktop.launcher import launch
        except ImportError as exc:
            raise SystemExit('Desktop dependencies missing. Install with: python -m pip install -e ".[desktop]"') from exc
        return launch(args.project,args.global_dir)
    try:
        result=asyncio.run(execute(args))
        if args.command=="board" and not args.json:
            print(f"{'TICKET':9} {'STATE':13} {'RUN':17} TITLE")
            for ticket in result:print(f"{ticket['key']:9} {ticket['state']:13} {(ticket.get('execution_state') or '-'):17} {ticket['title']}")
        elif isinstance(result,str) and not args.json:print(result)
        else:print(json.dumps(result,indent=2,ensure_ascii=False,default=str))
    except KeyboardInterrupt:
        print("\nEngine stopped. History and worktrees were retained. Review Recovery before resuming uncertain operations.",file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"AgileCode: {exc}",file=sys.stderr)
        raise SystemExit(1)


if __name__=="__main__":main()
