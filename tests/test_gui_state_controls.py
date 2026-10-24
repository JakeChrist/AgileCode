"""Real Qt/QML interaction tests for explicit control state and operational UI.

The signal-boundary backend below is a UI harness, not model/live-provider QA.
"""
import json
import os
from pathlib import Path

import pytest
from test_message_submission_qml import ui, qt, role_ui, send, rows, saved
from test_conversation_qml import QObject, QQuickItem, QTest, value, invoke, wait_for
from test_tool_settings_qml import click, visual_item, send_snapshot
from agilecode.core.types import Settings


def test_composer_widths_do_not_depend_on_scrollbar_available_width():
    source = (Path(__file__).parents[1] / "src/agilecode/desktop/qml/Main.qml").read_text()
    for composer in ("pmComposer", "discussionComposer", "ticketCorrectionComposer"):
        assert composer + ".availableWidth" not in source
        assert "width: Math.max(0, " + composer + ".width - 14)" in source


def screenshot(root, name):
    directory = os.environ.get("AGILECODE_SCREENSHOT_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        QTest.qWait(220)
        assert root.grabWindow().save(str(Path(directory) / (name + ".png")))


def event(backend, event_kind, session, **data):
    backend.event.emit(json.dumps({"kind": event_kind, "session_id": session, "data": data}))


def test_autonomy_on_settings_explicit_state_and_save_boundary(ui):
    root, backend = ui
    send_snapshot(backend, Settings(autonomy="restricted").model_dump(mode="json"))
    root.setProperty("currentPage", 5)
    autonomy = root.findChild(QQuickItem, "autonomySwitch")
    assert value(autonomy, "text") == "Full tool autonomy: Off"
    click(root, autonomy)
    assert value(autonomy, "text") == "Full tool autonomy: On (unsaved)"
    invoke(root, "saveToolSettings")
    assert backend.latest("settings")[2]["settings"]["autonomy"] == "restricted"
    invoke(root, "saveBasicSettings")
    assert backend.latest("settings")[2]["settings"]["autonomy"] == "full"
    screenshot(root, "settings-on-off-unsaved")
    # An authoritative refresh clears the visual unsaved state.
    root.setProperty("settingsReloadRequested", True)
    send_snapshot(backend, Settings(autonomy="full").model_dump(mode="json"), "updated")
    assert value(autonomy, "text") == "Full tool autonomy: On"
    for name in ("mergeSwitch", "reasoningOpenSwitch", "lightSwitch"):
        assert ": On" in value(root.findChild(QQuickItem, name), "text") or ": Off" in value(root.findChild(QQuickItem, name), "text")
    screenshot(root, "settings-on-off-saved")
    on_dark = str(value(value(autonomy, "indicator"), "color"))
    off_dark = str(value(value(root.findChild(QQuickItem, "mergeSwitch"), "indicator"), "color"))
    assert on_dark != off_dark
    root.setProperty("settingsReloadRequested", True)
    send_snapshot(backend, Settings(autonomy="full", theme="light").model_dump(mode="json"), "light")
    QTest.qWait(40)
    assert str(value(value(autonomy, "indicator"), "color")) != on_dark
    screenshot(root, "settings-on-off-light")
    root.setProperty("currentPage", 6)
    QTest.qWait(10)
    assert not autonomy.isVisible()


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_compaction_is_status_only_and_clears_after_finish_or_cancel(ui, role):
    root, backend = ui
    composer, view, session, _ = role_ui(root, role)
    prefix = {"pm_message": "ProjectManager", "feedback": "Model", "discuss": "Discussion"}[role]
    event(backend, "ConversationTurnStarted", session, turn_id="turn-old", activity_revision=1)
    event(backend, prefix + "GenerationStarted", session, request_id="summary", turn_id="turn-old", purpose="context_preparation")
    assert value(view, "processingText") == "Compressing context…"
    delta_kind = "ModelTextDelta" if role == "feedback" else prefix + "Delta"
    event(backend, delta_kind, session, kind="text", request_id="summary", turn_id="turn-old", purpose="context_preparation", value="SECRET INTERNAL SUMMARY")
    assert rows(view) == []
    assert value(view, "liveText") == ""
    event(backend, prefix + "GenerationFinished", session, request_id="summary", turn_id="turn-old", purpose="context_preparation")
    assert value(view, "processingText") == ""
    event(backend, prefix + "GenerationStarted", session, request_id="summary-two", turn_id="turn-old", purpose="context_preparation")
    send(root, backend, composer, role, "Use this new instruction immediately")
    assert value(view, "processingText") == ""
    event(backend, delta_kind, session, kind="text", request_id="summary-two", turn_id="turn-old", purpose="context_preparation", value="LATE SUMMARY")
    assert value(view, "processingText") == ""
    assert len(rows(view)) == 1 and rows(view)[0]["body"]["role"] == "user"


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_new_input_freezes_old_prefix_and_rejects_late_turn_output(ui, role):
    root, backend = ui
    composer, view, session, _ = role_ui(root, role)
    prefix = {"pm_message": "ProjectManager", "feedback": "Model", "discuss": "Discussion"}[role]
    delta_kind = "ModelTextDelta" if role == "feedback" else prefix + "Delta"
    event(backend, "ConversationTurnStarted", session, turn_id="turn-old", activity_revision=1)
    event(backend, prefix + "GenerationStarted", session, request_id="old", turn_id="turn-old", purpose="conversation")
    event(backend, delta_kind, session, kind="text", request_id="old", turn_id="turn-old", purpose="conversation", value="Earlier prefix")
    command = send(root, backend, composer, role, "New input")
    history_property = {"pm_message": "pmTranscript", "feedback": "transcript", "discuss": "discussionTranscript"}[role]
    root.setProperty(history_property, [saved(command, "accepted-input")])
    assert rows(view)[-1]["id"] == "accepted-input"
    assert rows(view)[-1]["body"]["content"] == "New input"
    assert sum(row["body"]["role"] == "user" for row in rows(view)) == 1
    event(backend, delta_kind, session, kind="text", request_id="old", turn_id="turn-old", purpose="conversation", value=" INVALIDATED LATE OUTPUT")
    assert value(view, "liveText") == "Earlier prefix"
    assert value(view, "interrupted")
    event(backend, prefix + "GenerationFinished", session, request_id="old", turn_id="turn-old", purpose="conversation", stopped=False)
    assert value(view, "interrupted")
    assert any(row["body"].get("content") == "New input" for row in rows(view))
    event(backend, prefix + "GenerationStarted", session, request_id="new", turn_id="turn-new", purpose="conversation")
    event(backend, delta_kind, session, kind="text", request_id="new", turn_id="turn-new", purpose="conversation", value="New response")
    event(backend, "ConversationTurnInvalidated", session, turn_id="turn-old", reason="input")
    event(backend, delta_kind, session, kind="text", request_id="new", turn_id="turn-new", purpose="conversation", value=" continues")
    assert value(view, "liveText") == "New response continues"


def epic_detail(identity="EMPTY", title="An epic without tickets"):
    return {"epic": {"id": identity, "title": title, "description": "A planning scope", "completion_criteria": "Deliver the result", "status": "Active", "revision": 1}, "tickets": [], "ticket_count": 0, "completed_count": 0}


def test_epic_create_list_empty_detail_and_safe_delete(ui):
    root, backend = ui
    root.setProperty("engineReady", True)
    root.setProperty("pmPanelExpanded", False)
    click(root, root.findChild(QQuickItem, "manageEpics"))
    backend.respond(backend.latest("epic_list"), ok=True, data=[{**epic_detail()["epic"], "ticket_count": 0, "completed_count": 0}])
    empty_row = visual_item(root, "epicRow-EMPTY")
    wait_for(lambda: empty_row is not None and empty_row.isVisible())
    screenshot(root, "epics-empty-ticket-list")
    click(root, empty_row)
    backend.respond(backend.latest("epic_detail"), ok=True, data=epic_detail())
    assert value(root.findChild(QObject, "epicDetailsDialog"), "visible")
    click(root, visual_item(root, "deleteEpic"))
    before = len(backend.requests)
    click(root, visual_item(root, "cancelDeleteEpic"))
    assert len(backend.requests) == before
    click(root, visual_item(root, "deleteEpic"))
    screenshot(root, "epic-delete-confirmation")
    click(root, visual_item(root, "confirmDeleteEpic"))
    command = backend.latest("epic_delete")
    assert command[2] == {"epic_id": "EMPTY", "revision": 1}
    backend.respond(command, ok=True, data={"epic_id": "EMPTY", "deleted": True, "detached_ticket_count": 0, "detached_ticket_ids": []})
    backend.respond(backend.latest("epic_list"), ok=True, data=[])
    click(root, visual_item(root, "newEpicButton"))
    root.findChild(QObject, "createEpicIdField").setProperty("text", "SEARCH")
    root.findChild(QObject, "newEpicTitle").setProperty("text", "Improve search")
    click(root, visual_item(root, "createEpic"))
    command = backend.latest("epic_create")
    assert command[2]["epic_id"] == "SEARCH" and command[2]["title"] == "Improve search"
    backend.respond(command, ok=True, data=epic_detail("SEARCH", "Improve search"))
    backend.respond(backend.latest("epic_detail"), ok=True, data=epic_detail("SEARCH", "Improve search"))
    assert value(root.findChild(QObject, "epicTitleField"), "text") == "Improve search"


def test_pm_runtime_state_is_separate_from_unsaved_autonomy(ui):
    root, backend = ui
    root.setProperty("currentPage", 2)
    root.setProperty("pmState", {"status": "paused", "current_activity": "Idle"})
    root.setProperty("pmSettings", {"mode": "restricted", "wait_for_current_tickets": False, "answer_ticket_questions": True, "allow_archive": False, "allow_delete": False})
    invoke(root, "loadPMSettings")
    assert "Allow archive/unarchive tickets" in value(root.findChild(QObject, "pmArchive"), "text")
    mode = root.findChild(QObject, "pmMode")
    assert value(root.findChild(QObject, "pmRuntimeAutonomy"), "text") == "Autonomy: Restricted"
    mode.setProperty("currentIndex", 0)
    assert value(root, "pmState")["status"] == "paused"
    assert value(root.findChild(QObject, "pmRuntimeAutonomy"), "text") == "Autonomy: Restricted"
    for name, checked in (("pmWaitCurrent", False), ("pmAnswerQuestions", True), ("pmArchive", False), ("pmDelete", False)):
        control = root.findChild(QObject, name)
        assert value(control, "text").endswith(": " + ("On" if checked else "Off"))
        control.setProperty("checked", not checked)
        assert value(control, "text").endswith("(unsaved)")
    invoke(root, "savePMSettings")
    command = backend.latest("pm_settings")
    assert command[2]["settings"]["mode"] == "fully_automated"
    assert value(root, "pmState")["status"] == "paused"




def test_ticket_archive_and_unarchive_use_same_action_and_refresh(ui):
    root, backend = ui
    root.setProperty("currentPage", 0)
    root.setProperty("engineReady", True)
    invoke(root, "openTicket", "ticket-archive")
    button = root.findChild(QQuickItem, "ticketArchive")
    assert button is not None

    def show_ticket(archived):
        root.setProperty("detail", {
            "ticket": {
                "id": "ticket-archive", "key": "T-001", "title": "Retained ticket",
                "description": "", "state": "Backlog", "archived": archived,
            },
            "run": None, "execution": {}, "attempts": [], "dependencies": [],
        })

    show_ticket(False)
    assert value(button, "text") == "Archive"
    click(root, button)
    archive = backend.latest("archive")
    assert archive[2] == {"ticket": "ticket-archive", "archived": True}
    backend.respond(archive, ok=True, data={})
    assert backend.latest("board")[2]["archived"] is False
    assert backend.latest("detail")[2]["ticket"] == "ticket-archive"

    show_ticket(True)
    assert value(button, "text") == "Unarchive"
    click(root, button)
    unarchive = backend.latest("archive")
    assert unarchive[2] == {"ticket": "ticket-archive", "archived": False}
    backend.respond(unarchive, ok=True, data={})
    assert backend.latest("board")[2]["archived"] is False
    assert backend.latest("detail")[2]["ticket"] == "ticket-archive"
    assert value(button, "text") == "Unarchive"  # Until the refreshed detail arrives.


def test_cancelled_run_uses_existing_resume_button_after_unarchive(ui):
    root, backend = ui
    root.setProperty("currentPage", 0)
    root.setProperty("engineReady", True)
    invoke(root, "openTicket", "cancelled-ticket")

    def show(archived, can_resume):
        root.setProperty("detail", {
            "ticket": {
                "id": "cancelled-ticket", "key": "T-009",
                "title": "Retained work", "description": "",
                "state": "In Progress", "archived": archived,
            },
            "run": {"id": "original-run", "state": "cancelled"},
            "execution": {
                "run_id": "original-run", "state": "cancelled",
                "can_resume": can_resume, "can_pause": False,
            },
            "attempts": [], "dependencies": [],
        })

    button = root.findChild(QQuickItem, "ticketResumeExecution")
    assert button is not None
    show(True, False)
    assert not button.isVisible()
    show(False, True)
    assert button.isVisible()
    click(root, button)
    assert backend.latest("resume_execution")[2] == {"ticket": "cancelled-ticket"}


def test_pm_pause_resume_is_a_single_control_in_the_board_composer(ui):
    root, backend = ui
    root.setProperty("currentPage", 0)
    root.setProperty("engineReady", True)
    toggle = root.findChild(QQuickItem, "projectManagerPauseResume")
    send_button = root.findChild(QQuickItem, "projectManagerSend")
    assert toggle is not None and send_button is not None
    assert toggle.parentItem() == send_button.parentItem()
    assert value(toggle, "text") == "Pause"

    click(root, toggle)
    pause = backend.latest("pm_pause")
    assert not any(action in {"pause_all", "resume_all"} for _, action, _ in backend.requests)

    def snapshot(status):
        return {"state": {"status": status, "current_activity": status.title()},
                "settings": {"mode": "restricted", "max_concurrent_tickets": 4}, "profiles": []}

    backend.respond(pause, ok=True, data=snapshot("paused"))
    assert value(toggle, "text") == "Resume"
    click(root, toggle)
    resume = backend.latest("pm_resume")
    assert not any(action in {"pause_all", "resume_all"} for _, action, _ in backend.requests)
    backend.respond(resume, ok=True, data=snapshot("running"))
    assert value(toggle, "text") == "Pause"

    # The same button follows authoritative state changes, including those
    # caused by Pause All / Resume All, without dispatching a global action.
    backend.response.emit("external-pm-state", "pm_snapshot",
                          json.dumps({"ok": True, "data": snapshot("paused")}))
    assert value(toggle, "text") == "Resume"
    backend.response.emit("external-pm-state-2", "pm_snapshot",
                          json.dumps({"ok": True, "data": snapshot("running")}))
    assert value(toggle, "text") == "Pause"

    # Preserve the existing way to turn the PM back on after Turn Off.
    root.setProperty("pmState", {"status": "off", "current_activity": "Off"})
    assert value(toggle, "text") == "Turn On"
    click(root, toggle)
    assert backend.latest("pm_resume")[0] != resume[0]


def test_invalidated_discussion_cannot_revive_after_navigation(ui):
    root, backend = ui
    composer, view, session, _ = role_ui(root, "discuss")
    event(backend, "ConversationTurnStarted", session, turn_id="old-turn", activity_revision=1)
    event(backend, "DiscussionGenerationStarted", session, request_id="old-request", turn_id="old-turn", purpose="conversation")
    send(root, backend, composer, "discuss", "Switch direction")
    invoke(root, "selectDiscussion", {"id": "other", "session_id": "other-session", "title": "Other"})
    invoke(root, "selectDiscussion", {"id": "discussion", "session_id": session, "title": "Testing"})
    event(backend, "DiscussionDelta", session, kind="text", request_id="old-request", turn_id="old-turn", purpose="conversation", value="Stale output")
    assert value(view, "liveText") == ""
    assert all(row["body"].get("content") != "Stale output" for row in rows(view))


@pytest.mark.parametrize("status,expected", [("error", "Cancelled by user"), ("ok", "Returned")])
def test_tool_cancellation_is_not_presented_as_failure(qt, status, expected):
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlEngine, QQmlComponent
    from test_conversation_qml import QML
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "ToolCall.qml")))
    item = component.createWithInitialProperties({"callData": {"function": {"name": "run_command", "arguments": "{}"}},
        "resultBody": {"content": json.dumps({"status": status, "cancelled_by_user": True,
            "error": {"code": "interrupted", "message": "Cancelled by user. Partial effects may remain."}, "data": {}})}})
    assert item is not None, [e.toString() for e in component.errors()]
    assert value(item, "status") == expected
    assert not value(item, "failed")
    item.deleteLater()
    QTest.qWait(1)


@pytest.mark.parametrize("role,button_name,action", [
    ("pm_message", "projectManagerStop", "pm_stop"),
    ("feedback", "ticketStopGeneration", "stop_generation"),
    ("discuss", "discussionStop", "discussion_stop"),
])
@pytest.mark.parametrize("purpose", ["conversation", "context_preparation"])
def test_stop_click_rejects_queued_output_before_backend_ack(ui, role, button_name, action, purpose):
    root, backend = ui
    _, view, session, _ = role_ui(root, role)
    if role == "pm_message":
        root.setProperty("currentPage", 2)
    prefix = {"pm_message": "ProjectManager", "feedback": "Model", "discuss": "Discussion"}[role]
    delta_kind = "ModelTextDelta" if role == "feedback" else prefix + "Delta"
    event(backend, "ConversationTurnStarted", session, turn_id="stop-turn", activity_revision=1)
    event(backend, prefix + "GenerationStarted", session, request_id="stop-request", turn_id="stop-turn", purpose=purpose)
    event(backend, delta_kind, session, kind="text", request_id="stop-request", turn_id="stop-turn", purpose=purpose, value="Initial output")
    button = root.findChild(QQuickItem, button_name)
    assert button.isEnabled()
    click(root, button)
    assert backend.latest(action)[1] == action
    assert value(view, "processingText") == ""
    # No backend response or invalidation event has been delivered yet.
    event(backend, delta_kind, session, kind="text", request_id="stop-request", turn_id="stop-turn", purpose=purpose, value=" late queued output")
    event(backend, prefix + "GenerationStarted", session, request_id="stop-request", turn_id="stop-turn", purpose=purpose)
    assert value(view, "liveText") == ("Initial output" if purpose == "conversation" else "")
    assert value(view, "processingText") == ""
    assert value(view, "interrupted")
