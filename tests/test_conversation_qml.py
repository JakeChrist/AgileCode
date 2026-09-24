"""Real Qt checks for the conversation presentation; no model server is required."""
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal, Slot, QUrl, QMetaObject, Q_ARG, Qt, QPointF
from PySide6.QtGui import QGuiApplication, QTextFormat
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, qmlRegisterType
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from agilecode.desktop.chatstyle import ChatDocumentStyle, MarkdownPresentation

QML = Path(__file__).parents[1] / "src/agilecode/desktop/qml"


def invoke(obj, method, value=None):
    args = [] if value is None else [Q_ARG("QVariant", value)]
    assert QMetaObject.invokeMethod(obj, method, Qt.DirectConnection, *args)


def value(obj, name):
    result = obj.property(name)
    return result.toVariant() if hasattr(result, "toVariant") else result


def record(identity="answer", request="r1", **body):
    return {"id": identity, "seq": 1, "request_id": request, "body": {"role": "assistant", **body}}


def delta(kind, text, request="r1", session="session"):
    return {"kind": kind, "session_id": session, "data": {"request_id": request, "value": text}}


@pytest.fixture(scope="module")
def qt():
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    qmlRegisterType(ChatDocumentStyle, "AgileCode.Desktop", 1, 0, "ChatDocumentStyle")
    qmlRegisterType(MarkdownPresentation, "AgileCode.Desktop", 1, 0, "MarkdownPresentation")
    return app


@pytest.fixture
def view(qt):
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "ConversationView.qml")))
    window = QQuickWindow()
    window.resize(720, 560)
    item = component.createWithInitialProperties({"width": 720, "height": 560, "sessionId": "session"})
    assert item is not None, [error.toString() for error in component.errors()]
    item.setParentItem(window.contentItem())
    window.show()
    QTest.qWait(20)
    yield item, window
    window.close()
    item.setParentItem(None)
    item.deleteLater()
    QTest.qWait(1)
    assert not warnings


def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)


def message_item(view, identity="answer"):
    QTest.qWait(30)
    return next(item for item in visual_items(view) if item.objectName() == "chatMessage-" + identity)


def tool_item(message):
    return next(item.parentItem() for item in visual_items(message)
                if item.objectName() == "toolToggle" and item.isVisible())


@pytest.mark.parametrize("dark", [True, False])
def test_qt_markdown_and_theme_formatting(view, dark):
    item, window = view
    item.setProperty("dark", dark)
    markdown = "# Heading\n\n**bold** and *italic* with `inline code`.\n\n- bullet\n\n1. numbered\n\n```python\ndef answer():\n    return 42\n```\n\n[Qt docs](https://doc.qt.io/)"
    item.setProperty("messages", [record(content=markdown, reasoning_content="Returned reasoning.")])
    message = message_item(item)
    answer = message.findChild(QQuickItem, "answerText")
    doc = answer.property("textDocument").textDocument()
    assert "**bold**" not in doc.toPlainText()
    assert "```" not in doc.toPlainText()
    assert "def answer():" in doc.toPlainText()
    code_blocks, links = [], []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().hasProperty(QTextFormat.BlockCodeFence):
            code_blocks.append(block)
        fragments = block.begin()
        while not fragments.atEnd():
            fragment = fragments.fragment()
            if fragment.charFormat().isAnchor():
                links.append(fragment.charFormat())
            fragments += 1
        block = block.next()
    assert len(code_blocks) == 2
    assert code_blocks[0].blockFormat().background().color().name() == ("#101923" if dark else "#edf2f6")
    assert links[0].foreground().color().name() == ("#91caff" if dark else "#145fad")
    assert links[0].anchorHref() == "https://doc.qt.io/"
    assert not window.grabWindow().isNull()


def test_streams_stay_separate_and_reasoning_to_answer_keeps_same_live_row(view):
    item, window = view
    item.setProperty("messages", [record("old", "old-request", content="Previous answer.")])
    previous = message_item(item, "old")

    invoke(item, "appendDelta", delta("ModelReasoningDelta", "Inspect "))
    invoke(item, "appendDelta", delta("ModelReasoningDelta", "the source."))
    QTest.qWait(20)
    live = item.findChild(QQuickItem, "streamingMessage")
    assert live is not None
    reasoning = live.findChild(QQuickItem, "reasoningSection")
    assert not value(reasoning, "expanded")
    toggle = reasoning.findChild(QQuickItem, "reasoningToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    assert value(reasoning, "expanded")

    # Starting normal output must update the same live delegate rather than
    # replacing a resizing ListView footer and destroying disclosure state.
    invoke(item, "appendDelta", delta("ModelTextDelta", "The **answer** "))
    QTest.qWait(20)
    assert item.findChild(QQuickItem, "streamingMessage") is live
    assert value(reasoning, "expanded")
    assert not value(item, "followTail")
    invoke(item, "appendDelta", delta("ModelTextDelta", "is ready."))
    invoke(item, "appendDelta", delta("ModelTextDelta", "Other ticket", session="elsewhere"))

    assert value(item, "liveReasoning") == "Inspect the source."
    assert value(item, "liveText") == "The **answer** is ready."
    assert message_item(item, "old") is previous
    assert value(item, "count") == 2  # persisted row + one normal streaming row

    invoke(item, "appendDelta", delta("ModelReasoningDelta", " Done."))
    assert value(reasoning, "expanded")
    assert value(reasoning, "content").endswith("Done.")

    item.setProperty("messages", [record(content="Complete.", reasoning_content="Saved reasoning.")])
    QTest.qWait(20)
    assert not value(item, "hasLiveMessage")
    assert item.findChild(QQuickItem, "streamingMessage") is None
    invoke(item, "appendDelta", delta("ModelTextDelta", "Late duplicate"))
    assert not value(item, "hasLiveMessage")
    saved = message_item(item)
    assert value(saved, "reasoningText") == "Saved reasoning."


def test_older_snapshot_cannot_clear_a_newer_response(view):
    item, _ = view
    invoke(item, "appendDelta", delta("ModelTextDelta", "New turn", request="r2"))
    item.setProperty("messages", [record(request="r1", content="Earlier turn")])
    assert value(item, "liveText") == "New turn"
    item.setProperty("messages", [])
    item.setProperty("sessionId", "different-session")
    assert not value(item, "hasLiveMessage")
    assert value(item, "count") == 0


def test_disclosure_clicks_and_adjacent_tool_results(view):
    item, window = view
    call = {"id": "reused", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
    first = record("first", "r1", content="First", reasoning_content="Returned reasoning", tool_calls=[call])
    result = record("result", "r1", role="tool", tool_call_id="reused", content='{"status":"error","error":"Missing file"}')
    second = record("second", "r2", content="Second", tool_calls=[call])
    item.setProperty("messages", [first, result, second])
    first_item = message_item(item, "first")
    reasoning = first_item.findChild(QQuickItem, "reasoningSection")
    button = reasoning.findChild(QQuickItem, "reasoningToggle")
    point = button.mapToScene(button.boundingRect().center()).toPoint()
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
    assert value(reasoning, "expanded")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
    assert not value(reasoning, "expanded")
    assert value(item, "count") == 2  # The raw tool-result row is folded into its call.
    first_tool = tool_item(first_item)
    second_item = message_item(item, "second")
    second_tool = tool_item(second_item)
    assert value(first_tool, "status") == "Failed"
    assert value(second_tool, "status") == "Requested"
    toggle = first_tool.findChild(QQuickItem, "toolToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    assert value(first_tool, "expanded")
    assert "Missing file" in value(first_tool.findChild(QQuickItem, "toolDetails"), "text")
    item.setProperty("messages", [first, result, second, record("third", "r3", content="Third")])
    assert value(first_tool, "expanded")


def test_fragmented_tools_and_partial_response(view):
    item, _ = view
    invoke(item, "appendDelta", delta("ModelToolDelta", {"index": 0, "id": "c1", "function": {"name": "read_", "arguments": '{"path":'}}))
    invoke(item, "appendDelta", delta("ModelToolDelta", {"index": 0, "function": {"name": "file", "arguments": '"a.py"}'}}))
    calls = value(item, "liveToolCalls")
    assert calls[0]["function"] == {"name": "read_file", "arguments": '{"path":"a.py"}'}
    text = "A long response " * 1100
    partial = {**record(content=text), "partial": True}
    item.setProperty("messages", [partial])
    message = message_item(item)
    assert value(message, "partial")
    answer = message.findChild(QQuickItem, "answerText")
    assert answer.property("textDocument").textDocument().toPlainText().strip() == text.strip()


def test_main_loads_and_uses_existing_event_contract(qt):
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
            self.requests.append((action, json.loads(payload)))
            return str(len(self.requests))

    backend = Backend()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(QML / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    invoke(root, "selectSession", "session")
    view = root.findChild(QQuickItem, "conversationView")
    backend.event.emit(json.dumps(delta("ModelReasoningDelta", "Thinking")))
    backend.event.emit(json.dumps(delta("ModelTextDelta", "Answer")))
    assert value(view, "liveText") == "Answer"
    assert value(view, "liveReasoning") == "Thinking"
    backend.event.emit(json.dumps({"kind": "ConversationChanged", "session_id": "session", "data": {}}))
    assert value(view, "liveText") == "Answer"  # Remains visible until replacement is loaded.
    for _ in range(30):
        QTest.qWait(10)
        if len(backend.requests) > 1:
            break
    assert len(backend.requests) > 1
    envelope = json.dumps({"ok": True, "data": {"session": "session", "messages": [record(content="Answer", reasoning_content="Thinking")]}})
    backend.response.emit("1", "conversation", envelope)  # Stale snapshot is ignored.
    assert value(view, "hasLiveMessage")
    backend.response.emit(str(len(backend.requests)), "conversation", envelope)
    assert not value(view, "hasLiveMessage")
    assert value(view, "count") == 1
    root.setProperty("closingConfirmed", True)
    root.close()


def test_ticket_description_controls_are_inside_visible_inspector(qt):
    class Backend(QObject):
        response = Signal(str, str, str)
        event = Signal(str)
        ready = Signal(str)
        fatal = Signal(str)

        @Slot(str, str, result=str)
        def dispatch(self, action, payload):
            return "1"

    engine = QQmlApplicationEngine()
    backend = Backend()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(QML / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]

    long_description = (
        "## Objective\n\n"
        "Split the existing check/capture/attack tactics logic into dedicated modules "
        "under modules/tactics while preserving the current behavior and updating the GUI "
        "to display each module separately. "
    ) * 12
    root.setProperty("detail", {
        "ticket": {
            "id": "ticket-1",
            "key": "T-001",
            "state": "Ready",
            "title": "Long description ticket",
            "description": long_description,
        },
        "run": None,
        "source_discussion": None,
        "waits": [],
        "dependencies": [],
        "attempts": [],
        "jobs": [],
        "checkpoints": [],
        "integrations": [],
    })

    inspector = root.findChild(QObject, "ticketInspector")
    assert inspector is not None
    invoke(inspector, "open")
    QTest.qWait(60)

    overview = root.findChild(QQuickItem, "ticketOverviewScroll")
    toggle = root.findChild(QQuickItem, "ticketDescriptionToggle")
    preview = root.findChild(QQuickItem, "ticketDescriptionPreview")
    assert overview is not None and toggle is not None and preview is not None
    assert toggle.isVisible()

    overview_left = overview.mapToScene(QPointF(0, 0)).x()
    overview_right = overview_left + overview.width()
    toggle_left = toggle.mapToScene(QPointF(0, 0)).x()
    assert toggle_left >= overview_left - 1
    assert toggle_left + toggle.width() <= overview_right + 1
    assert preview.width() <= overview.width() + 1
    assert value(preview, "lineCount") > 1

    QTest.mouseClick(
        root,
        Qt.LeftButton,
        Qt.NoModifier,
        toggle.mapToScene(toggle.boundingRect().center()).toPoint(),
    )
    QTest.qWait(30)
    assert value(root, "ticketDescriptionExpanded")
    expanded = root.findChild(QQuickItem, "ticketDescriptionScroll")
    assert expanded is not None and expanded.isVisible()
    assert expanded.width() <= overview.width() + 1

    rendered = root.findChild(QQuickItem, "ticketDescriptionText")
    assert rendered is not None
    document = rendered.property("textDocument").textDocument()
    assert "## Objective" not in document.toPlainText()
    assert "Objective" in document.toPlainText()
    assert "**" not in document.toPlainText()

    root.setProperty("closingConfirmed", True)
    root.close()


def test_streaming_and_interrupted_prose_never_exposes_markdown_syntax(view):
    item, _ = view
    invoke(item, "appendDelta", delta("ModelTextDelta", "## Heading\n\n**bold"))
    QTest.qWait(20)
    live = item.findChild(QQuickItem, "streamingMessage")
    answer = live.findChild(QQuickItem, "answerText")
    plain = answer.property("textDocument").textDocument().toPlainText()
    assert "##" not in plain
    assert "**" not in plain
    assert "Heading" in plain
    assert "bold" in plain

    item.setProperty("messages", [{
        **record(content="## Heading\n\n**bold"),
        "partial": True,
    }])
    QTest.qWait(20)
    interrupted = message_item(item)
    answer = interrupted.findChild(QQuickItem, "answerText")
    plain = answer.property("textDocument").textDocument().toPlainText()
    assert "##" not in plain
    assert "**" not in plain


def test_reasoning_and_finish_step_render_as_prose_not_raw_markdown(view):
    item, window = view
    finish = {
        "id": "finish-1",
        "function": {
            "name": "finish_step",
            "arguments": json.dumps({
                "status": "PASS",
                "summary": "## Summary\n**Done**",
                "output": "### Result\n- item one\n- item two",
            }),
        },
    }
    assistant = record(
        "finished",
        "r-finish",
        content="",
        reasoning_content="## Reasoning\n**Checked** everything.",
        tool_calls=[finish],
    )
    result = record(
        "finish-result",
        "r-finish",
        role="tool",
        tool_call_id="finish-1",
        content=json.dumps({
            "status": "ok",
            "data": {
                "status": "PASS",
                "summary": "## Summary\n**Done**",
                "output": "### Result\n- item one\n- item two",
            },
        }),
    )
    item.setProperty("messages", [assistant, result])
    message = message_item(item, "finished")

    reasoning = message.findChild(QQuickItem, "reasoningSection")
    toggle = reasoning.findChild(QQuickItem, "reasoningToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    QTest.qWait(20)
    reasoning_text = reasoning.findChild(QQuickItem, "reasoningText")
    reasoning_plain = reasoning_text.property("textDocument").textDocument().toPlainText()
    assert "##" not in reasoning_plain
    assert "**" not in reasoning_plain

    tool = tool_item(message)
    tool_toggle = tool.findChild(QQuickItem, "toolToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     tool_toggle.mapToScene(tool_toggle.boundingRect().center()).toPoint())
    QTest.qWait(20)
    summary = tool.findChild(QQuickItem, "toolProseSummary")
    output = tool.findChild(QQuickItem, "toolProseOutput")
    assert summary is not None and output is not None
    assert "##" not in summary.property("textDocument").textDocument().toPlainText()
    assert "**" not in summary.property("textDocument").textDocument().toPlainText()
    output_plain = output.property("textDocument").textDocument().toPlainText()
    assert "###" not in output_plain
    assert "- item" not in output_plain


def test_ticket_level_execution_recovery_controls_are_visible_and_dispatch(qt):
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
            self.requests.append((action, json.loads(payload)))
            return str(len(self.requests))

    backend = Backend()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(QML / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    root.setProperty("selectedTicket", "ticket-1")
    root.setProperty("detail", {
        "ticket": {
            "id": "ticket-1",
            "key": "T-001",
            "state": "In Progress",
            "title": "Interrupted ticket",
            "description": "Continue this work.",
        },
        "run": {
            "id": "run-1",
            "state": "paused",
            "error": "stopped",
            "branch": "agile/T-001",
            "base_oid": "abc",
            "workspace": "/tmp/worktree",
        },
        "execution": {
            "run_id": "run-1",
            "state": "paused",
            "can_resume": True,
            "can_restart_step": True,
            "current_attempt_id": "attempt-1",
        },
        "source_discussion": None,
        "waits": [],
        "dependencies": [],
        "jobs": [],
        "checkpoints": [],
        "integrations": [],
        "attempts": [{
            "id": "attempt-1",
            "session_id": "session-1",
            "step_id": "implement",
            "number": 1,
            "status": "interrupted",
            "phase": "development",
            "result": None,
            "member": {"name": "Developer"},
            "model": {"model": "fixture"},
        }],
    })

    inspector = root.findChild(QObject, "ticketInspector")
    invoke(inspector, "open")
    QTest.qWait(60)

    recovery = root.findChild(QQuickItem, "ticketExecutionRecovery")
    resume = root.findChild(QQuickItem, "ticketResumeExecution")
    reset = root.findChild(QQuickItem, "ticketResetCurrentStep")
    assert recovery is not None and recovery.isVisible()
    assert resume is not None and resume.isVisible()
    assert reset is not None and reset.isVisible()
    assert value(resume, "text") == "Resume execution"
    assert value(reset, "text") == "Reset current step"
    assert root.findChild(QQuickItem, "attemptContinueButton") is None
    assert root.findChild(QQuickItem, "attemptResetButton") is None
    assert root.findChild(QQuickItem, "conversationContinueInterrupted") is None

    QTest.mouseClick(
        root, Qt.LeftButton, Qt.NoModifier,
        resume.mapToScene(resume.boundingRect().center()).toPoint(),
    )
    assert backend.requests[-1] == ("resume_execution", {"ticket": "ticket-1"})

    QTest.mouseClick(
        root, Qt.LeftButton, Qt.NoModifier,
        reset.mapToScene(reset.boundingRect().center()).toPoint(),
    )
    assert backend.requests[-1] == ("restart_current_step", {"ticket": "ticket-1"})

    root.setProperty("closingConfirmed", True)
    root.close()

