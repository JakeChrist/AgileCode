"""A small headless adapter. It invokes exactly the same services as the desktop UI."""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from copy import deepcopy
from pathlib import Path
from .core.application import Application
from .core.types import AgileCodeError, Conflict, Settings, Team, ProviderConfig, uid


def parser():
    p=argparse.ArgumentParser(prog="agilecode",description="Headless ticket-based coding agents; run `agilecode gui` for the desktop interface.")
    p.add_argument("-p","--project",default=".",help="Original Git project root")
    p.add_argument("--global-dir",default=None,help="Override ~/.agilecode (useful for isolated tests)")
    p.add_argument("--json",action="store_true",help="Print structured JSON instead of the board table")
    sub=p.add_subparsers(dest="command",required=True)
    q=sub.add_parser("init",help="Initialize project data and add .agilecode/ to .gitignore");q.add_argument("path",nargs="?")
    sub.add_parser("gui",help="Launch the replaceable QML desktop frontend")
    q=sub.add_parser("board");q.add_argument("--filter",default="");q.add_argument("--archived",action="store_true")
    q=sub.add_parser("create");q.add_argument("title");q.add_argument("--description",default="");q.add_argument("--description-file");q.add_argument("--team");q.add_argument("--process");q.add_argument("--parent");q.add_argument("--provider");q.add_argument("--ready",action="store_true");q.add_argument("--request-id",help="Reuse this ID only when retrying the same creation")
    q=sub.add_parser("show");q.add_argument("ticket")
    q=sub.add_parser("move");q.add_argument("ticket");q.add_argument("state",choices=["Backlog","Ready","In Progress","Done","Cancelled"]);q.add_argument("--request-id",help="Reuse this ID only when retrying the same move")
    for name in ("run","resume"):
        q=sub.add_parser(name,help="Run and keep the engine alive, handling questions/approvals interactively")
        q.add_argument("ticket");q.add_argument("--non-interactive",action="store_true");q.add_argument("--allow-dirty-base",action="store_true");q.add_argument("--request-id")
    for name in ("pause","cancel","delete","cleanup","archive","reopen"):
        q=sub.add_parser(name);q.add_argument("ticket")
    q=sub.add_parser("feedback");q.add_argument("ticket");q.add_argument("text");q.add_argument("--request-id")
    q=sub.add_parser("answer");q.add_argument("wait_id");q.add_argument("text");q.add_argument("--non-interactive",action="store_true")
    q=sub.add_parser("conversation");q.add_argument("session");cursors=q.add_mutually_exclusive_group();cursors.add_argument("--after",type=int,default=None);cursors.add_argument("--before",type=int,default=None)
    q=sub.add_parser("diff");q.add_argument("ticket")
    q=sub.add_parser("checkpoint");q.add_argument("ticket");q.add_argument("--label",default="User checkpoint")
    q=sub.add_parser("restore-checkpoint");q.add_argument("ticket");q.add_argument("checkpoint");q.add_argument("--expected-tree",required=True)
    q=sub.add_parser("preview-checkpoint");q.add_argument("ticket");q.add_argument("checkpoint")
    q=sub.add_parser("prepare-merge");q.add_argument("ticket");q.add_argument("--rebuild",action="store_true");q.add_argument("--non-interactive",action="store_true")
    q=sub.add_parser("merge");q.add_argument("integration");q.add_argument("--candidate",required=True);q.add_argument("--target",required=True)
    q=sub.add_parser("settings");q.add_argument("action",choices=["show","save","reset-project"]);q.add_argument("file",nargs="?");q.add_argument("--scope",choices=["global","project"]);q.add_argument("--for-edit",action="store_true",help="Show editable values together with their original conflict-checking snapshot")
    q=sub.add_parser("teams");q.add_argument("action",choices=["list","export","save"],default="list",nargs="?");q.add_argument("value",nargs="?");q.add_argument("--scope",choices=["global","project"]);q.add_argument("--for-edit",action="store_true",help="Export an edit envelope; change only its value before saving")
    q=sub.add_parser("providers");q.add_argument("action",choices=["list","export","save","default","delete"],default="list",nargs="?");q.add_argument("value",nargs="?");q.add_argument("--make-default",action="store_true");q.add_argument("--for-edit",action="store_true",help="Export an edit envelope; change only its value before saving")
    sub.add_parser("models");sub.add_parser("skills")
    q=sub.add_parser("job");q.add_argument("id");q.add_argument("--offset",type=int,default=0)
    q=sub.add_parser("backup");q.add_argument("destination")
    q=sub.add_parser("restore-backup");q.add_argument("source");q.add_argument("destination")
    q=sub.add_parser("export-ticket");q.add_argument("ticket");q.add_argument("destination")
    q=sub.add_parser("discussions");q.add_argument("--archived",action="store_true")
    q=sub.add_parser("new-discussion");q.add_argument("--title")
    q=sub.add_parser("discuss");q.add_argument("question");q.add_argument("--discussion");q.add_argument("--request-id")
    return p


def _edit_document(kind, value, expected, **context):
    """CLI transport only; shared operations own all merge/conflict decisions."""
    return {"format": "agilecode-edit", "version": 1, "kind": kind,
            "value": deepcopy(value), "expected": deepcopy(expected), **context}


def _read_edit_input(path, kind):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise AgileCodeError("Provide a JSON object or a saved --for-edit envelope.")
    if data.get("format") != "agilecode-edit":
        return data, None
    if data.get("version") != 1 or data.get("kind") != kind:
        raise AgileCodeError("Unsupported or mismatched edit envelope. Export this definition again with --for-edit.")
    if not isinstance(data.get("value"), dict) or not isinstance(data.get("expected"), dict):
        raise AgileCodeError("Edit envelope must retain its original expected snapshot; edit only value.")
    if kind in {"settings", "team"} and data.get("scope") not in {"global", "project"}:
        raise AgileCodeError("Edit envelope must identify its original global or project scope.")
    if kind == "provider" and "expected_default" not in data:
        raise AgileCodeError("Provider edit envelope must retain expected_default.")
    return data["value"], data


def _edit_scope(requested, envelope, default):
    captured = envelope["scope"] if envelope else None
    if requested is not None and captured is not None and requested != captured:
        raise AgileCodeError("Save scope differs from the edit snapshot. Export the intended scope with --for-edit.")
    return requested or captured or default


async def interact(app, operation, non_interactive=False):
    """Keep one engine owner alive while servicing durable human interaction."""
    task=asyncio.ensure_future(operation)
    prompted=set()
    try:
        while not task.done():
            for wait in await app.pending_waits():
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
    execution_commands={"run","resume","answer","pause","cancel","delete","cleanup","archive",
                        "feedback","checkpoint","restore-checkpoint","prepare-merge","merge","discuss"}
    app=Application(args.path if args.command=="init" and args.path else args.project,args.global_dir,
                    execution=args.command in execution_commands)
    request_id=getattr(args,"request_id",None) or uid()
    unsub=None
    try:
        opened=await app.open(initialize=args.command=="init")
        if args.command=="init":return opened
        if args.command in {"run","resume","answer","prepare-merge"}:
            def progress(event):
                if event.kind in {"StepAttemptStarted","StepAttemptCompleted","IntegrationReady","IntegrationBlocked","ExecutionCompleted","ExecutionInterrupted"}:
                    print(f"{event.kind}: "+json.dumps(event.data,ensure_ascii=False),file=sys.stderr)
            unsub=app.events.subscribe(progress)
        command=args.command
        if command=="board":return await app.board(args.filter,include_archived=args.archived)
        if command=="create":
            description=Path(args.description_file).read_text() if args.description_file else args.description
            parent=(await app.find_ticket(args.parent))["id"] if args.parent else None
            ticket=await app.create_ticket(args.title,description,team_ref=args.team,process_id=args.process,parent_id=parent,provider_id=args.provider,request_id=request_id)
            if args.ready:ticket=await app.move_ticket(ticket["id"],"Ready",request_id=request_id+":ready")
            return ticket
        if command=="show":return await app.ticket_detail(args.ticket)
        if command=="move":
            # This one-shot host records the same move without pretending it
            # remains alive afterward. The shared result reports unavailable execution.
            return await app.move_ticket(args.ticket,args.state,request_id=request_id)
        if command in {"run","resume"}:
            async def run():
                if command=="run":record=await app.start_ticket(args.ticket,allow_dirty_base=args.allow_dirty_base,request_id=request_id)
                else:
                    await app.resume_run(args.ticket,request_id=request_id)
                    ticket=await app.find_ticket(args.ticket);record=await app.selected_run(ticket["id"])
                result=await app.wait_for_run(record["id"])
                ticket=await app.find_ticket(args.ticket)
                merge_task=app.integration_tasks.get(ticket["id"])
                if merge_task:await merge_task
                return await app.ticket_detail(args.ticket)
            return await interact(app,run(),args.non_interactive)
        if command=="pause":await app.pause_run(args.ticket);return {"paused":args.ticket}
        if command=="cancel":await app.cancel_run(args.ticket);return {"cancelled":args.ticket}
        if command=="delete":return await app.delete_ticket(args.ticket)
        if command=="cleanup":return await app.retry_cleanup(args.ticket)
        if command=="archive":await app.archive_ticket(args.ticket);return {"archived":args.ticket}
        if command=="reopen":return await app.move_ticket(args.ticket,"Backlog",request_id=request_id)
        if command=="feedback":await app.submit_feedback(args.ticket,args.text,input_id=request_id);return {"queued":True}
        if command=="answer":
            wait=await app.get_wait(args.wait_id)
            async def answer():
                await app.answer(args.wait_id,args.text)
                run=await app.selected_run(wait["ticket_id"])
                if run:await app.wait_for_run(run["id"])
                merge_task=app.integration_tasks.get(wait["ticket_id"])
                if merge_task:await merge_task
                return await app.ticket_detail(wait["ticket_id"])
            return await interact(app,answer(),args.non_interactive)
        if command=="conversation":return await app.conversation(args.session,after=args.after,before=args.before)
        if command=="diff":return await app.diff(args.ticket)
        if command=="checkpoint":return await app.checkpoint(args.ticket,args.label)
        if command=="preview-checkpoint":
            return await app.preview_checkpoint(args.ticket,args.checkpoint)
        if command=="restore-checkpoint":return await app.restore_checkpoint(args.ticket,args.checkpoint,args.expected_tree)
        if command=="prepare-merge":return await interact(app,app.prepare_integration(args.ticket,rebuild=args.rebuild),args.non_interactive)
        if command=="merge":return await app.approve_integration(args.integration,args.candidate,args.target)
        if command=="settings":
            if args.action=="show":
                if not args.for_edit:return app.config.settings.model_dump(mode="json")
                scope=args.scope or "project"
                snapshot=await app.config.snapshot(scope)
                return _edit_document("settings",snapshot["effective"],snapshot,scope=scope)
            if args.action=="reset-project":
                await app.config.reset_project();return {"inheriting":"global settings"}
            if not args.file:raise AgileCodeError("Provide a JSON settings file.")
            value,envelope=_read_edit_input(args.file,"settings")
            scope=_edit_scope(args.scope,envelope,"project")
            settings=Settings.model_validate(value)
            if envelope:
                expected=envelope["expected"]
            else:
                if (await app.config.snapshot(scope))["values"]:
                    raise Conflict("Existing settings require a captured edit snapshot. Run settings show --for-edit --scope " + scope + ", edit only value, then save that file.")
                expected={"values":{},"effective":{}}
            await app.config.save(settings,scope,expected=expected);return {"saved":scope}
        if command=="teams":
            if args.action=="list":return await app.catalog.list_teams()
            if not args.value:raise AgileCodeError("Provide a team reference to export or a JSON file to save.")
            if args.action=="export":
                value=(await app.catalog.get_team(args.value)).model_dump(mode="json")
                if not args.for_edit:return value
                scope=args.value.split(":",1)[0]
                if scope not in {"global","project"}:
                    raise AgileCodeError("Built-in teams are immutable. Export normally and save a new global or project copy.")
                return _edit_document("team",value,value,scope=scope)
            value,envelope=_read_edit_input(args.value,"team")
            scope=_edit_scope(args.scope,envelope,"global")
            try:
                return {"saved":await app.catalog.save_team(Team.model_validate(value),scope,expected=envelope["expected"] if envelope else None)}
            except Conflict as exc:
                if envelope:raise
                raise Conflict(str(exc) + " To edit an existing team, run teams export " + scope + ":" + str(value.get("id","TEAM_ID")) + " --for-edit, edit only value, then save that file.") from exc
        if command=="providers":
            if args.action=="list":return app.config.providers_snapshot()
            if not args.value:raise AgileCodeError("Provide a provider JSON file or provider ID.")
            if args.action=="export":
                pool=app.config.providers_snapshot()
                value=next((item for item in pool["providers"] if item["id"]==args.value),None)
                if value is None:raise AgileCodeError("Provider " + repr(args.value) + " is not available in the global Provider Pool.")
                if not args.for_edit:return value
                return _edit_document("provider",value,value,expected_default=pool["default_provider_id"])
            if args.action=="save":
                value,envelope=_read_edit_input(args.value,"provider")
                expected_default=envelope["expected_default"] if envelope else app.config.providers_snapshot()["default_provider_id"]
                try:
                    return app.config.save_provider(ProviderConfig.model_validate(value),make_default=args.make_default,
                        expected=envelope["expected"] if envelope else None,expected_default=expected_default)
                except Conflict as exc:
                    if envelope:raise
                    raise Conflict(str(exc) + " To edit an existing provider, run providers export " + str(value.get("id","PROVIDER_ID")) + " --for-edit, edit only value, then save that file.") from exc
            if args.action=="default":return app.config.set_default_provider(args.value)
            return app.config.delete_provider(args.value)
        if command=="models":return await app.models()
        if command=="skills":return await asyncio.to_thread(app.catalog.list_skills,["*"])
        if command=="job":return await app.job_output(args.id,args.offset)
        if command=="backup":return await app.backup(args.destination)
        if command=="export-ticket":return await app.export_ticket(args.ticket,args.destination)
        if command=="discussions":return await app.discussion.list(include_archived=args.archived)
        if command=="new-discussion":return await app.discussion.create(args.title)
        if command=="discuss":
            discussion=await app.discussion.get(args.discussion) if args.discussion else await app.discussion.create()
            return await app.discussion.ask(discussion["id"],args.question,input_id=request_id)
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
        print("\nEngine stopped. History and worktrees were retained. Interrupted tool results are recorded in the ticket conversation.",file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"AgileCode: {exc}",file=sys.stderr)
        raise SystemExit(1)


if __name__=="__main__":main()
