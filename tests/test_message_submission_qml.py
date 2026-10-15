"""Actual QML submission, durable-acceptance and failed-draft regression tests."""
import asyncio
import concurrent.futures
import json
from queue import Queue
from time import monotonic
from types import SimpleNamespace

import pytest

from test_conversation_qml import (
    QObject, Signal, Slot, QUrl, Qt, QQuickItem, QQmlApplicationEngine,
    QQmlExpression, QQmlEngine, QTest, QML, qt, value, invoke, wait_for,
    message_item,
)


class Backend(QObject):
    response = Signal(str, str, str)
    event = Signal(str)
    ready = Signal(str)
    fatal = Signal(str)

    def __init__(self):
        super().__init__()
        self.requests = []

    @Slot(str, str, result=str)
    def dispatch(self, action, payload):
        identity = "command-" + str(len(self.requests) + 1)
        self.requests.append((identity, action, json.loads(payload)))
        return identity

    def latest(self, action):
        return next(item for item in reversed(self.requests) if item[1] == action)

    def respond(self, command, **envelope):
        self.response.emit(command[0], command[1], json.dumps(envelope))


@pytest.fixture
def ui(qt):
    backend = Backend()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(QML / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    root.setProperty("pmPanelExpanded", True)
    root.setProperty("pmPanelHeight", 400)
    root.setProperty("pmState", {"status": "running", "current_activity": "Testing"})
    yield root, backend
    root.setProperty("closingConfirmed", True)
    root.close()
    QTest.qWait(5)


def role_ui(root, role):
    if role == "pm_message":
        root.setProperty("currentPage", 0)
        return (root.findChild(QQuickItem, "projectManagerInput"),
                root.findChild(QQuickItem, "projectManagerConversation"), "project-manager", "ProjectManagerChanged")
    if role == "discuss":
        root.setProperty("currentPage", 1)
        invoke(root, "selectDiscussion", {"id": "discussion", "session_id": "discussion-session", "title": "Testing", "archived": False})
        return (root.findChild(QQuickItem, "discussionInput"),
                root.findChild(QQuickItem, "projectDiscussionView"), "discussion-session", "DiscussionChanged")
    invoke(root, "openTicket", "ticket")
    root.setProperty("detail", {"ticket": {"id": "ticket", "title": "Testing", "key": "T-1", "description": "", "state": "In Progress"},
        "controls": {}, "run": {"id": "run", "state": "running"}, "execution": {"run_id": "run", "can_pause": True},
        "attempts": [{"id": "attempt", "session_id": "ticket-session", "step_id": "implementation",
            "number": 1, "status": "running", "phase": "development", "result": None,
            "member": {"name": "Developer"}, "model": {"model": "test"}}]})
    invoke(root, "selectSession", "ticket-session")
    return (root.findChild(QQuickItem, "ticketCorrectionInput"),
            root.findChild(QQuickItem, "conversationView"), "ticket-session", "FeedbackQueued")


def send(root, backend, composer, action, text):
    composer.setProperty("text", text)
    composer.forceActiveFocus()
    QTest.qWait(30)
    QTest.keyClick(root, Qt.Key_Return)
    QTest.qWait(30)
    command = backend.latest(action)
    assert command[2]["text"] == text.strip()
    assert value(composer, "text") == ""
    return command


def rows(view):
    expression = QQmlExpression(QQmlEngine.contextForObject(view), view, """
        (function() { var result = []; for (var i = 0; i < count; ++i) {
            var row = model.get(i); result.push({id: row.recordId, body: row.messageBody,
                status: row.deliveryStatus, submission: row.submissionId, canRestore: row.canRestore});
        } return JSON.stringify(result); })()
    """)
    result, _ = expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    return json.loads(result)


def receipt(command, session, identity="saved-input"):
    return {"input_id": command[2].get("request_id", command[0]), "session_id": session,
            "message_id": identity, "id": "receipt-" + identity, "version": 1, "included": False}


def accept(backend, kind, item):
    backend.event.emit(json.dumps({"kind": kind, "session_id": item["session_id"],
        "data": {"input": item, "discussion_id": "discussion"}}))


def saved(command, identity="saved-input", partial=True):
    return {"id": identity, "seq": 1, "request_id": command[2].get("request_id", command[0]),
            "body": {"role": "user", "content": command[2]["text"]}, "partial": partial}


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_all_composers_show_pending_input_before_backend_acceptance(ui, role):
    root, backend = ui
    composer, view, session, kind = role_ui(root, role)
    command = send(root, backend, composer, role, "  Keep this precise direction.  ")
    assert rows(view) == [{"id": "__submission__" + command[0],
        "body": {"role": "user", "content": "Keep this precise direction."},
        "status": "sending", "submission": command[0], "canRestore": False}]
    pending = message_item(view, "__submission__" + command[0])
    assert "Sending" in value(pending.findChild(QQuickItem, "messageRole"), "text")
    accept(backend, kind, receipt(command, session))
    assert rows(view)[0]["status"] == "queued"
    assert len(value(root, "conversationSubmissions")) == 1
    assert "Queued" in value(pending.findChild(QQuickItem, "messageRole"), "text")


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_failed_unaccepted_send_restores_exact_draft(ui, role):
    root, backend = ui
    composer, view, _, _ = role_ui(root, role)
    original = "  Preserve these spaces\nand this second line.  "
    command = send(root, backend, composer, role, original)
    backend.respond(command, ok=False, error="Rejected before acceptance")
    assert value(composer, "text") == original
    assert rows(view) == []
    assert not value(root, "conversationSubmissions")[0]["accepted"]


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
@pytest.mark.parametrize("acceptance_source", ["event", "error", "history"])
def test_post_acceptance_error_never_restores_or_duplicates_input(ui, role, acceptance_source):
    root, backend = ui
    composer, view, session, kind = role_ui(root, role)
    history_property = {"pm_message": "pmTranscript", "feedback": "transcript", "discuss": "discussionTranscript"}[role]
    command = send(root, backend, composer, role, "Accepted direction")
    item = receipt(command, session)
    if acceptance_source == "event":
        accept(backend, kind, item)
    elif acceptance_source == "history":
        root.setProperty(history_property, [saved(command)])
    backend.respond(command, ok=False, error="Old tool cleanup failed",
                    **({"input": item} if acceptance_source == "error" else {}))
    assert value(composer, "text") == ""
    assert len(rows(view)) == 1
    assert not rows(view)[0]["canRestore"]
    root.setProperty(history_property, [saved(command)])
    accept(backend, kind, item)  # Duplicate or late acceptance is harmless.
    assert len(rows(view)) == 1 and rows(view)[0]["id"] == "saved-input"
    assert value(composer, "text") == ""
    assert value(root, "conversationSubmissions") == []


@pytest.mark.parametrize("edit_restored", [False, True])
def test_receipt_after_error_withdraws_only_untouched_restored_draft(ui, edit_restored):
    root, backend = ui
    composer, view, session, kind = role_ui(root, "pm_message")
    command = send(root, backend, composer, "pm_message", "Original input")
    backend.respond(command, ok=False, error="Transport response failed")
    assert value(composer, "text") == "Original input"
    if edit_restored:
        composer.setProperty("text", "New text the user has edited")
    accept(backend, kind, receipt(command, session))
    assert value(composer, "text") == ("New text the user has edited" if edit_restored else "")
    assert len(rows(view)) == 1 and rows(view)[0]["status"] == "queued"
    root.setProperty("pmTranscript", [saved(command)])
    assert len(rows(view)) == 1


def test_failure_preserves_newer_draft_and_exposes_old_text_for_recovery(ui):
    root, backend = ui
    composer, view, _, _ = role_ui(root, "pm_message")
    command = send(root, backend, composer, "pm_message", "First draft")
    composer.setProperty("text", "Newer unsent draft")
    backend.respond(command, ok=False, error="Rejected")
    assert value(composer, "text") == "Newer unsent draft"
    assert rows(view)[0]["body"]["content"] == "First draft"
    assert rows(view)[0]["canRestore"]
    invoke(root, "restoreConversationDraft", command[0])
    assert value(composer, "text") == "Newer unsent draft"
    composer.setProperty("text", "")
    invoke(root, "restoreConversationDraft", command[0])
    assert value(composer, "text") == "First draft" and rows(view) == []


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_multiple_sends_and_out_of_order_responses_are_identity_correlated(ui, role):
    root, backend = ui
    composer, view, session, kind = role_ui(root, role)
    history_property = {"pm_message": "pmTranscript", "feedback": "transcript", "discuss": "discussionTranscript"}[role]
    first = send(root, backend, composer, role, "Identical direction")
    second = send(root, backend, composer, role, "Identical direction")
    composer.setProperty("text", "Third unsent draft")
    assert len(rows(view)) == 2
    accept(backend, kind, receipt(second, session, "saved-second"))
    backend.respond(second, ok=False, error="Second accepted; execution failed")
    backend.respond(first, ok=False, error="First not accepted")
    assert value(composer, "text") == "Third unsent draft"
    assert [row["status"] for row in rows(view)] == ["failed", "queued"]
    root.setProperty(history_property, [saved(second, "saved-second")])
    assert len(rows(view)) == 2
    assert sum(row["canRestore"] for row in rows(view)) == 1
    assert sum(row["id"] == "saved-second" for row in rows(view)) == 1


def test_last_failed_send_restores_without_overwriting_on_older_failure(ui):
    root, backend = ui
    composer, view, _, _ = role_ui(root, "pm_message")
    first = send(root, backend, composer, "pm_message", "First")
    second = send(root, backend, composer, "pm_message", "Second")
    backend.respond(second, ok=False, error="Second failed")
    backend.respond(first, ok=False, error="First failed")
    assert value(composer, "text") == "Second"
    assert [row["body"]["content"] for row in rows(view)] == ["First"]


def test_retry_of_untouched_restored_draft_reuses_durable_input_identity(ui):
    root, backend = ui
    composer, view, session, kind = role_ui(root, "pm_message")
    first = send(root, backend, composer, "pm_message", "Retry exactly once")
    backend.respond(first, ok=False, error="Uncertain transport error")
    # Dismiss the error dialog and use the real composer without changing its draft.
    QTest.keyClick(root, Qt.Key_Escape)
    composer.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Return)
    second = backend.latest("pm_message")
    assert second[0] != first[0]
    assert second[2]["request_id"] == first[0]
    assert len(rows(view)) == 1
    backend.respond(first, ok=False, error="Late duplicate response")
    assert value(composer, "text") == ""
    accept(backend, kind, receipt(second, session))
    root.setProperty("pmTranscript", [saved(second)])
    backend.respond(second, ok=True, data={"input": receipt(second, session)})
    assert len(rows(view)) == 1 and rows(view)[0]["id"] == "saved-input"


def test_partial_user_receipt_is_queued_and_only_assistant_is_interrupted(ui):
    root, backend = ui
    _, view, _, _ = role_ui(root, "pm_message")
    root.setProperty("pmTranscript", [
        {"id": "user", "seq": 1, "body": {"role": "user", "content": "Keep **exact** direction"}, "partial": True},
        {"id": "assistant", "seq": 2, "body": {"role": "assistant", "content": "Stopped response"}, "partial": True},
    ])
    user = message_item(view, "user")
    assistant = message_item(view, "assistant")
    assert value(user.findChild(QQuickItem, "messageRole"), "text") == "You · Queued"
    assert not value(user, "partial")
    assert "Interrupted response" in value(assistant.findChild(QQuickItem, "messageRole"), "text")
    assert value(assistant, "partial")


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_lookup_fails", [False, True])
@pytest.mark.parametrize("action,payload", [
    ("pm_message", {}), ("discuss", {"discussion": "d"}), ("feedback", {"ticket": "t"})])
async def test_bridge_error_envelope_carries_shared_durable_receipt(qt, monkeypatch, action, payload, receipt_lookup_fails):
    from agilecode.desktop.bridge import Bridge
    item = {"input_id": "original-input", "message_id": "saved", "session_id": "session"}
    queried = []

    async def fail(*args, **kwargs):
        raise RuntimeError("Failure after acceptance")

    async def read_receipt(identity):
        queried.append(identity)
        if receipt_lookup_fails:
            raise RuntimeError("Receipt lookup unavailable")
        return item

    app = SimpleNamespace(project_manager=SimpleNamespace(message=fail), discussion=SimpleNamespace(ask=fail),
                          submit_feedback=fail, conversation_input_receipt=read_receipt)
    bridge = Bridge.__new__(Bridge)
    QObject.__init__(bridge)
    bridge.worker = SimpleNamespace(app=app, loop=asyncio.get_running_loop(), ready=SimpleNamespace(is_set=lambda: True))
    bridge.windows, bridge.outbox = [], Queue()
    captured = []

    def schedule(coroutine, loop):
        future = concurrent.futures.Future()
        captured.append((coroutine, future))
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)
    command = bridge.dispatch(action, json.dumps({**payload, "text": "input", "request_id": "original-input"}))
    coroutine, future = captured[0]
    try:
        await coroutine
    except RuntimeError as error:
        future.set_exception(error)
    kind, (identity, returned_action, envelope) = bridge.outbox.get_nowait()
    assert (kind, identity, returned_action) == ("response", command, action)
    assert envelope == {"ok": False, "error": "Failure after acceptance",
                        **({"input_status": "unknown"} if receipt_lookup_fails else {"input": item})}
    assert queried == ["original-input"]


@pytest.mark.parametrize("role,generation_prefix,stop_name,stop_action", [
    ("pm_message", "ProjectManager", "projectManagerStop", "pm_stop"),
    ("discuss", "Discussion", "discussionStop", "discussion_stop"),
    ("feedback", "Model", "ticketStopGeneration", "stop_generation"),
])
def test_whole_turn_keeps_stop_and_correction_available_between_model_rounds(
        ui, role, generation_prefix, stop_name, stop_action):
    root, backend = ui
    composer, view, session, _ = role_ui(root, role)
    backend.event.emit(json.dumps({"kind": "ConversationTurnStarted", "session_id": session,
        "data": {"turn_id": "turn-1", "activity_revision": 1}}))
    for suffix in ["GenerationStarted", "GenerationFinished"]:
        backend.event.emit(json.dumps({"kind": generation_prefix + suffix, "run_id": "run",
            "session_id": session, "data": {"discussion_id": "discussion", "stopped": False}}))
    backend.event.emit(json.dumps({"kind": "ToolStarted", "session_id": session, "data": {}}))
    if role == "pm_message":
        root.setProperty("currentPage", 2)
    stop = root.findChild(QQuickItem, stop_name)
    QTest.qWait(40)
    assert stop.isVisible() and value(stop, "enabled")
    QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier,
                    stop.mapToScene(stop.boundingRect().center()).toPoint())
    assert backend.latest(stop_action)[1] == stop_action
    if role == "pm_message":
        root.setProperty("currentPage", 0)
    command = send(root, backend, composer, role, "Correct during tool work")
    if role == "feedback":
        assert command[2]["interrupt"] is True
    assert len(rows(view)) == 1
    assert value(root, "activeConversationTurns")[session] == "turn-1"


def test_turn_revisions_reject_stale_finish_and_snapshot(ui):
    root, backend = ui
    def event(kind, turn, revision):
        backend.event.emit(json.dumps({"kind": kind, "session_id": "project-manager",
            "data": {"turn_id": turn, "activity_revision": revision}}))
    event("ConversationTurnStarted", "old-turn", 1)
    event("ConversationTurnFinished", "old-turn", 2)
    event("ConversationTurnStarted", "new-turn", 3)
    event("ConversationTurnFinished", "old-turn", 2)
    invoke(root, "applyConversationActivity", {"active_conversations": {},
        "conversation_activity_revisions": {"project-manager": 2}})
    assert value(root, "activeConversationTurns") == {"project-manager": "new-turn"}
    assert value(root, "pmBusy")
    event("ConversationTurnFinished", "new-turn", 4)
    invoke(root, "applyConversationActivity", {"active_conversations": {"project-manager": "new-turn"},
        "conversation_activity_revisions": {"project-manager": 3}})
    assert value(root, "activeConversationTurns") == {}
    assert not value(root, "pmBusy")
    invoke(root, "applyConversationActivity", {"active_conversations": {"other-session": "background-turn"},
        "conversation_activity_revisions": {"other-session": 1}})
    assert value(root, "activeConversationTurns") == {"other-session": "background-turn"}


def test_failed_discussion_send_stays_with_its_session_after_navigation(ui):
    root, backend = ui
    composer, view, _, _ = role_ui(root, "discuss")
    command = send(root, backend, composer, "discuss", "First discussion input")
    invoke(root, "selectDiscussion", {"id": "other", "session_id": "other-session", "title": "Other", "archived": False})
    composer.setProperty("text", "Draft for the other discussion")
    backend.respond(command, ok=False, error="Original discussion send failed")
    assert value(composer, "text") == "Draft for the other discussion"
    assert rows(view) == []
    invoke(root, "selectDiscussion", {"id": "discussion", "session_id": "discussion-session", "title": "Testing", "archived": False})
    assert len(rows(view)) == 1
    assert rows(view)[0]["body"]["content"] == "First discussion input"
    assert rows(view)[0]["canRestore"]
    assert value(composer, "text") == "Draft for the other discussion"


def test_unresolved_acceptance_stays_visible_without_claiming_input_was_unsent(ui):
    root, backend = ui
    composer, view, session, kind = role_ui(root, "pm_message")
    command = send(root, backend, composer, "pm_message", "Possibly accepted")
    backend.respond(command, ok=False, error="Storage unavailable", input_status="unknown")
    assert value(composer, "text") == ""
    assert rows(view)[0]["status"] == "unknown"
    pending = message_item(view, "__submission__" + command[0])
    assert "Send status unknown" in value(pending.findChild(QQuickItem, "messageRole"), "text")
    accept(backend, kind, receipt(command, session))
    assert rows(view)[0]["status"] == "queued"
    assert not rows(view)[0]["canRestore"]
    assert value(composer, "text") == ""


def test_unknown_send_recovery_retries_with_original_input_id(ui):
    root, backend = ui
    composer, view, session, kind = role_ui(root, "pm_message")
    first = send(root, backend, composer, "pm_message", "Recover uncertain send")
    backend.respond(first, ok=False, error="Cannot read receipt", input_status="unknown")
    QTest.keyClick(root, Qt.Key_Escape)
    invoke(root, "restoreConversationDraft", first[0])
    assert value(composer, "text") == "Recover uncertain send"
    composer.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Return)
    second = backend.latest("pm_message")
    assert second[0] != first[0] and second[2]["request_id"] == first[0]
    assert len(rows(view)) == 1
    accept(backend, kind, receipt(second, session))
    root.setProperty("pmTranscript", [saved(second)])
    backend.respond(second, ok=True, data={"input": receipt(second, session)})
    assert len(rows(view)) == 1


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_new_input_stays_visible_beside_old_stream_while_cleanup_is_delayed(ui, role):
    root, backend = ui
    composer, view, session, kind = role_ui(root, role)
    delta_kind = {"pm_message": "ProjectManagerDelta", "feedback": "ModelTextDelta", "discuss": "DiscussionDelta"}[role]
    backend.event.emit(json.dumps({"kind": delta_kind, "session_id": session,
        "data": {"kind": "text", "request_id": "old-stream", "value": "Existing response still settling"}}))
    command = send(root, backend, composer, role, "New direction during cleanup")
    assert value(view, "liveText") == "Existing response still settling"
    assert len(rows(view)) == 2
    accept(backend, kind, receipt(command, session))
    assert [row["body"]["content"] for row in rows(view)] == [
        "Existing response still settling", "New direction during cleanup"]
    assert rows(view)[1]["status"] == "queued"
    history_property = {"pm_message": "pmTranscript", "feedback": "transcript", "discuss": "discussionTranscript"}[role]
    root.setProperty(history_property, [
        {"id": "old-response", "seq": 1, "request_id": "old-stream", "partial": True,
         "body": {"role": "assistant", "content": "Existing response still settling"}},
        {**saved(command), "seq": 2},
    ])
    assert len(rows(view)) == 2
    assert not value(view, "hasLiveMessage")
    backend.respond(command, ok=True, data={"input": receipt(command, session)})
    assert len(rows(view)) == 2
    assert value(composer, "text") == ""


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_explicit_send_reveals_pending_row_after_scrolling_up(ui, role):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication, QWheelEvent
    root, backend = ui
    composer, view, session, kind = role_ui(root, role)
    history_property = {"pm_message": "pmTranscript", "feedback": "transcript", "discuss": "discussionTranscript"}[role]
    root.setProperty(history_property, [
        {"id": "history-" + str(i), "seq": i + 1, "request_id": "history-request-" + str(i),
         "body": {"role": "assistant", "content": "Earlier response.\n\n" * 4}}
        for i in range(35)
    ])
    QTest.qWait(300)
    def scroll_up():
        previous = value(view, "contentY")
        point = view.mapToScene(view.boundingRect().center())
        QTest.mouseMove(root, point.toPoint())
        event = QWheelEvent(point, root.mapToGlobal(point.toPoint()), QPoint(), QPoint(0, 720),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        # Qt deduplicates timestamp-zero synthetic wheel events; separate
        # gestures need distinct timestamps just like native input events.
        event.setTimestamp(int(monotonic() * 1000))
        QGuiApplication.sendEvent(root, event)
        QTest.qWait(170)
        assert value(view, "contentY") < previous - 20
        assert not value(view, "followTail")
    scroll_up()
    command = send(root, backend, composer, role, "Show my newly sent direction")
    wait_for(lambda: value(view, "followTail") and value(view, "atYEnd"))
    pending = message_item(view, "__submission__" + command[0])
    assert pending.y() + pending.height() > value(view, "contentY")
    assert pending.y() < value(view, "contentY") + view.height()
    # Background acceptance must still respect a new deliberate reading position.
    scroll_up()
    accept(backend, kind, receipt(command, session))
    QTest.qWait(100)
    assert not value(view, "followTail")
