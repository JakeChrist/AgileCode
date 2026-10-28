"""Real Qt checks for the conversation presentation; no model server is required."""
import json
import os
from pathlib import Path
from time import monotonic

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal, Slot, QUrl, QMetaObject, Q_ARG, Qt, QPointF
from PySide6.QtGui import QGuiApplication, QTextFormat
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression, QQmlEngine, qmlRegisterType
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


def wait_for(predicate, timeout_ms=1000):
    """Process Qt events until the observable condition holds, or fail."""
    deadline = monotonic() + timeout_ms / 1000
    while not predicate() and monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "Qt condition did not become true before the deadline"


def record(identity="answer", request="r1", **body):
    return {"id": identity, "seq": 1, "request_id": request, "body": {"role": "assistant", **body}}


def delta(kind, text, request="r1", session="session"):
    return {"kind": kind, "session_id": session, "data": {"request_id": request, "value": text}}


@pytest.fixture(scope="module")
def qt():
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    # Use an isolated settings namespace while exercising persisted QML UI preferences.
    app.setOrganizationName("AgileCodeTests")
    app.setApplicationName("AgileCodeTests")
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
    assert QTest.qWaitForWindowExposed(window)
    QTest.qWait(60)
    yield item, window
    window.close()
    item.setParentItem(None)
    item.deleteLater()
    QTest.qWait(1)
    assert not warnings


def visual_items(item, keep=None):
    # Keep Qt wrappers alive during assertions: PySide can invalidate child
    # wrappers when a temporary parent wrapper is collected.
    if keep is None:
        keep = getattr(item, "_visual_refs", [])
        item._visual_refs = keep
    keep.append(item)
    yield item
    for child in item.childItems():
        yield from visual_items(child, keep)


def text_document(item):
    item._document_ref = item.property("textDocument")
    return item._document_ref.textDocument()


def message_item(view, identity="answer"):
    invoke(view, "forceLayout")
    QTest.qWait(100)
    return next(item for item in visual_items(view) if item.objectName() == "chatMessage-" + identity)


def tool_item(message):
    return next(item.parentItem() for item in visual_items(message)
                if item.objectName() == "toolToggle" and item.isVisible())


def tool_field(tool, name):
    return next((item for item in visual_items(tool) if item.objectName() == name), None)


@pytest.mark.parametrize("dark", [True, False])
def test_qt_markdown_and_theme_formatting(view, dark):
    item, window = view
    item.setProperty("dark", dark)
    markdown = "# Heading\n\n**bold** and *italic* with `inline code`.\n\n- bullet\n\n1. numbered\n\n```python\ndef answer():\n    return 42\n```\n\n[Qt docs](https://doc.qt.io/)"
    item.setProperty("messages", [record(content=markdown, reasoning_content="Returned reasoning.")])
    message = message_item(item)
    answer = message.findChild(QQuickItem, "answerText")
    doc = text_document(answer)
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


def test_reasoning_can_start_expanded_by_display_preference(view):
    item, _ = view
    item.setProperty("reasoningExpandedByDefault", True)
    item.setProperty("messages", [record(content="Answer.", reasoning_content="Reasoning.")])
    reasoning = message_item(item).findChild(QQuickItem, "reasoningSection")
    assert value(reasoning, "expanded")

    item.setProperty("sessionId", "next-session")
    assert value(item, "liveReasoningExpanded")


def test_streams_stay_separate_and_reasoning_to_answer_keeps_same_live_row(view):
    item, window = view
    item.setProperty("messages", [record("old", "old-request", content="Previous answer.")])
    previous = message_item(item, "old")

    invoke(item, "appendDelta", delta("ModelReasoningDelta", "Inspect "))
    invoke(item, "appendDelta", delta("ModelReasoningDelta", "the source."))
    invoke(item, "forceLayout")
    QTest.qWait(100)
    live = next((child for child in visual_items(item) if child.objectName() == "streamingMessage"), None)
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
    assert next(child for child in visual_items(item) if child.objectName() == "streamingMessage") is live
    assert value(reasoning, "expanded")
    assert not value(item, "followTail")  # Explicitly opening reasoning pauses following.
    invoke(item, "appendDelta", delta("ModelTextDelta", "is ready."))
    invoke(item, "appendDelta", delta("ModelTextDelta", "Other ticket", session="elsewhere"))

    assert value(item, "liveReasoning") == "Inspect the source."
    assert value(item, "liveText") == "The **answer** is ready."
    assert message_item(item, "old") is previous
    assert value(item, "count") == 2  # persisted row + one normal streaming row

    invoke(item, "appendDelta", delta("ModelReasoningDelta", " Done."))
    # The view deliberately batches delegate/document updates every 100 ms.
    # Wait for the bound text, without forcing its private flush function.
    wait_for(lambda: value(reasoning, "content") == "Inspect the source. Done.")
    assert value(reasoning, "expanded")
    assert next(child for child in visual_items(item) if child.objectName() == "streamingMessage") is live
    answer = live.findChild(QQuickItem, "answerText")
    assert text_document(answer).toPlainText() == "The answer is ready."

    item.setProperty("messages", [record(content="Complete.", reasoning_content="Saved reasoning.")])
    QTest.qWait(20)
    assert not value(item, "hasLiveMessage")
    assert item.findChild(QQuickItem, "streamingMessage") is None
    invoke(item, "appendDelta", delta("ModelTextDelta", "Late duplicate"))
    assert not value(item, "hasLiveMessage")
    saved = message_item(item)
    assert value(saved, "reasoningText") == "Saved reasoning."


def test_initial_history_positions_at_tail(view):
    item, _ = view
    rows = [
        record(f"message-{i}", f"request-{i}", content=("Line of history.\n" * 10))
        for i in range(24)
    ]
    item.setProperty("messages", rows)
    QTest.qWait(180)
    bottom = value(item, "originY") + max(0, value(item, "contentHeight") - value(item, "height"))
    assert value(item, "atYEnd")


def test_jump_to_tail_restores_tail_following(view):
    item, _ = view
    rows = [
        record(f"history-{i}", f"request-{i}", content=("History line.\n" * 12))
        for i in range(24)
    ]
    item.setProperty("messages", rows)
    QTest.qWait(160)
    item.setProperty("followTail", False)
    item.setProperty("contentY", value(item, "originY"))
    invoke(item, "jumpToTail")
    QTest.qWait(120)
    bottom = value(item, "originY") + max(0, value(item, "contentHeight") - value(item, "height"))
    assert value(item, "followTail")
    assert value(item, "atYEnd")


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
    QTest.qWait(80)
    point = button.mapToScene(button.boundingRect().center()).toPoint()
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
    assert not value(reasoning, "expanded")
    assert value(item, "count") == 2  # The raw tool-result row is folded into its call.
    first_tool = tool_item(first_item)
    second_item = message_item(item, "second")
    second_tool = tool_item(second_item)
    assert value(first_tool, "status") == "Failed"
    assert value(second_tool, "status") == "Requested"
    summary = first_tool.findChild(QQuickItem, "toolResultSummary")
    assert summary.isVisible() and value(summary, "text") == "Missing file"
    assert first_tool.findChild(QQuickItem, "toolRawToggle") is None
    toggle = first_tool.findChild(QQuickItem, "toolToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    assert value(first_tool, "expanded")
    # One expansion exposes complete inputs and error fields, with no raw view.
    path = tool_field(first_tool, "toolValue-inputs/path")
    error = tool_field(first_tool, "toolValue-result/error")
    assert path.isVisible() and value(path, "text") == "a.py"
    assert error.isVisible() and value(error, "text") == "Missing file"
    assert not summary.isVisible()  # The error is not duplicated above its field.
    assert first_tool.findChild(QQuickItem, "toolDetails") is None
    item.setProperty("messages", [first, result, second, record("third", "r3", content="Third")])
    assert value(first_tool, "expanded")
    assert value(second_tool, "status") == "Requested"
    assert not second_tool.findChild(QQuickItem, "toolResultSummary").isVisible()


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
    assert text_document(answer).toPlainText().strip() == text.strip()


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
    envelope = json.dumps({"ok": True, "data": {"session": "session", "messages": [record(content="Answer", reasoning_content="Thinking")], "has_older": False}})
    backend.response.emit("1", "conversation", envelope)  # Stale snapshot is ignored.
    assert value(view, "hasLiveMessage")
    backend.response.emit(str(len(backend.requests)), "conversation", envelope)
    assert not value(view, "hasLiveMessage")
    assert value(view, "count") == 1
    root.setProperty("detail", {"controls": {}, "pending_feedback": [{"text": "Keep <literal> text and fix the parser"}]})
    notice = root.findChild(QQuickItem, "ticketFeedbackFollowup")
    assert notice is not None
    assert "Keep <literal> text and fix the parser" in value(notice, "text")
    assert "need follow-up" in value(notice, "text")
    expression = QQmlExpression(QQmlEngine.contextForObject(notice), notice, "textFormat === Text.PlainText")
    assert expression.evaluate()[0] is True
    root.setProperty("closingConfirmed", True)
    root.close()



def test_pm_conversation_is_single_collapsible_board_panel(qt):
    """Collapsing the PM panel must not destroy its live transcript."""
    class Backend(QObject):
        response = Signal(str, str, str)
        event = Signal(str)
        ready = Signal(str)
        fatal = Signal(str)

        @Slot(str, str, result=str)
        def dispatch(self, action, payload):
            return "1"

    backend = Backend()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(QML / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    panel = root.findChild(QQuickItem, "boardProjectManagerPanel")
    view = root.findChild(QQuickItem, "projectManagerConversation")
    resize_handle = root.findChild(QQuickItem, "projectManagerResizeHandle")
    assert panel is not None and view is not None and resize_handle is not None
    assert len(root.findChildren(QQuickItem, "projectManagerConversation")) == 1
    ancestor = view.parentItem()
    while ancestor is not None and ancestor is not panel:
        ancestor = ancestor.parentItem()
    assert ancestor is panel

    original_expanded = value(root, "pmPanelExpanded")
    original_height = value(root, "pmPanelHeight")
    try:
        root.setProperty("pmPanelExpanded", False)
        QTest.qWait(80)
        assert not view.isVisible()
        assert not resize_handle.isVisible()

        backend.event.emit(json.dumps({
            "kind": "ProjectManagerDelta", "session_id": "project-manager",
            "data": {"kind": "text", "request_id": "pm-stream",
                     "value": "Live progress while collapsed."}
        }))
        assert value(view, "liveText") == "Live progress while collapsed."

        root.setProperty("pmPanelExpanded", True)
        QTest.qWait(100)
        assert view.isVisible()
        assert resize_handle.isVisible()
        assert value(view, "liveText") == "Live progress while collapsed."

        root.setProperty("pmPanelHeight", 265)
        QTest.qWait(75)
        small_height = panel.height()
        root.setProperty("pmPanelHeight", 350)
        QTest.qWait(75)
        assert panel.height() > small_height + 30

        root.setProperty("currentPage", 2)
        QTest.qWait(40)
        assert not view.isVisible()
        backend.event.emit(json.dumps({
            "kind": "ProjectManagerDelta", "session_id": "project-manager",
            "data": {"kind": "text", "request_id": "pm-stream",
                     "value": " Still streaming."}
        }))
        assert value(view, "liveText").endswith("Still streaming.")
        root.setProperty("currentPage", 0)
        QTest.qWait(75)
        assert view.isVisible()
        assert value(view, "liveText").endswith("Still streaming.")
    finally:
        root.setProperty("pmPanelExpanded", original_expanded)
        root.setProperty("pmPanelHeight", original_height)
        QTest.qWait(80)
        root.setProperty("closingConfirmed", True)
        root.close()

def test_ticket_description_preview_opens_full_reading_dialog(qt):
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
        "to display each module separately.\n\n"
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
    wait_for(lambda: value(inspector, "opened"))

    overview = root.findChild(QQuickItem, "ticketOverviewScroll")
    open_button = root.findChild(QQuickItem, "ticketDescriptionOpen")
    preview = root.findChild(QQuickItem, "ticketDescriptionPreview")
    preview_card = root.findChild(QQuickItem, "ticketDescriptionPreviewCard")
    assert overview is not None and open_button is not None and preview is not None
    assert open_button.isVisible() and preview.isVisible()

    overview_left = overview.mapToScene(QPointF(0, 0)).x()
    overview_right = overview_left + overview.width()
    button_left = open_button.mapToScene(QPointF(0, 0)).x()
    assert button_left >= overview_left - 1
    assert button_left + open_button.width() <= overview_right + 1
    assert preview.width() <= overview.width() + 1
    assert preview_card.height() == 118
    assert value(preview_card, "clip")
    assert value(preview, "maximumLineCount") == 6
    assert value(preview, "text") == long_description

    QTest.mouseClick(
        root,
        Qt.LeftButton,
        Qt.NoModifier,
        open_button.mapToScene(open_button.boundingRect().center()).toPoint(),
    )
    dialog = root.findChild(QObject, "ticketDescriptionDialog")
    assert dialog is not None
    wait_for(lambda: value(dialog, "opened"))
    scroll = root.findChild(QQuickItem, "ticketDescriptionDialogScroll")
    assert scroll is not None and scroll.isVisible()
    assert value(dialog, "modal")
    assert value(dialog, "width") <= root.width() - 120
    assert value(dialog, "height") <= root.height() - 120

    rendered = root.findChild(QQuickItem, "ticketDescriptionDialogText")
    assert rendered is not None and rendered.isVisible()
    assert value(rendered, "readOnly") and value(rendered, "selectByMouse")
    assert rendered.width() <= scroll.width() + 1
    document = text_document(rendered)
    assert "## Objective" not in document.toPlainText()
    assert document.toPlainText().count("Objective") == 12
    assert "**" not in document.toPlainText()
    assert value(scroll, "contentHeight") > value(scroll, "availableHeight")

    # Dismissing/reopening must preserve the inspector and the complete text.
    QTest.keyClick(root, Qt.Key_Escape)
    wait_for(lambda: not value(dialog, "visible"))
    assert value(inspector, "opened")
    QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier,
                     open_button.mapToScene(open_button.boundingRect().center()).toPoint())
    wait_for(lambda: value(dialog, "opened"))
    assert text_document(rendered).toPlainText().count("Objective") == 12

    root.setProperty("closingConfirmed", True)
    root.close()


def test_streaming_and_interrupted_prose_never_exposes_markdown_syntax(view):
    item, _ = view
    invoke(item, "appendDelta", delta("ModelTextDelta", "## Heading\n\n**bold"))
    invoke(item, "forceLayout")
    QTest.qWait(100)
    live = next((child for child in visual_items(item) if child.objectName() == "streamingMessage"), None)
    answer = live.findChild(QQuickItem, "answerText")
    plain = text_document(answer).toPlainText()
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
    plain = text_document(answer).toPlainText()
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
    reasoning_plain = text_document(reasoning_text).toPlainText()
    assert "##" not in reasoning_plain
    assert "**" not in reasoning_plain

    tool = tool_item(message)
    tool_toggle = tool.findChild(QQuickItem, "toolToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     tool_toggle.mapToScene(tool_toggle.boundingRect().center()).toPoint())
    QTest.qWait(20)
    summary = tool_field(tool, "toolProseSummary")
    output = tool_field(tool, "toolProseOutput")
    assert summary is not None and output is not None
    assert "##" not in text_document(summary).toPlainText()
    assert "**" not in text_document(summary).toPlainText()
    output_plain = text_document(output).toPlainText()
    assert "###" not in output_plain
    assert "- item" not in output_plain


def test_ticket_level_resume_button_is_visible_and_dispatches_resume(qt):
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
            "state": "cancelled",
            "error": "legacy automatic cancellation",
            "branch": "agile/T-001",
            "base_oid": "abc",
            "workspace": "/tmp/worktree",
        },
        "execution": {
            "run_id": "run-1",
            "state": "cancelled",
            "can_resume": True,
            "can_pause": False,
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
    QTest.qWait(350)  # Wait for the drawer opening animation before pointer input.

    resume = root.findChild(QQuickItem, "ticketResumeExecution")
    assert resume is not None and resume.isVisible()
    assert value(resume, "text") == "Resume"
    assert root.findChild(QQuickItem, "ticketExecutionRecovery") is None
    assert root.findChild(QQuickItem, "ticketResetCurrentStep") is None
    assert root.findChild(QQuickItem, "attemptContinueButton") is None
    assert root.findChild(QQuickItem, "attemptResetButton") is None
    assert root.findChild(QQuickItem, "conversationContinueInterrupted") is None

    QTest.mouseClick(
        root, Qt.LeftButton, Qt.NoModifier,
        resume.mapToScene(resume.boundingRect().center()).toPoint(),
    )
    assert backend.requests[-1] == ("resume_execution", {"ticket": "ticket-1"})

    root.setProperty("closingConfirmed", True)
    root.close()



def wheel(window, dy):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    point = QPointF(350, 260)
    event = QWheelEvent(point, window.mapToGlobal(point.toPoint()), QPoint(), QPoint(0, dy),
                        Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    QGuiApplication.sendEvent(window, event)
    QTest.qWait(160)


def visible_anchor(item):
    rows = [child for child in visual_items(item)
            if child.objectName().startswith('chatMessage-') and child.y() <= value(item, 'contentY') + 30
            and child.y() + child.height() > value(item, 'contentY') + 30]
    assert rows
    row = rows[-1]
    return row.objectName(), row.y() - value(item, 'contentY')


def test_rapid_stream_follows_without_waiting_for_generation_to_stop(view):
    item, _ = view
    item.setProperty('messages', [record(f'history-{i}', f'h-{i}', content='History\n\n' * 6) for i in range(15)])
    QTest.qWait(250)
    before = value(item, 'contentY')
    for i in range(70):
        invoke(item, 'appendDelta', delta('ModelTextDelta', 'A growing answer line.\n'))
        QTest.qWait(10)  # Faster than the 48 ms tail timer, with no idle gap.
    assert value(item, 'followTail')
    assert value(item, 'contentY') > before + 300
    QTest.qWait(160)
    assert value(item, 'atYEnd')


def test_small_manual_scroll_near_bottom_does_not_reenable_following(view):
    item, _ = view
    rows = [
        record(f"line-{i}", f"req-{i}", content="History line.\n\n" * 10)
        for i in range(25)
    ]
    item.setProperty("messages", rows)
    QTest.qWait(200)
    bottom = value(item, "originY") + max(0, value(item, "contentHeight") - value(item, "height"))
    invoke(item, "beginInteraction")
    item.setProperty("contentY", bottom - 6)
    invoke(item, "finishInteraction")
    QTest.qWait(100)
    assert not value(item, "followTail")
    invoke(item, "appendDelta", delta("ModelTextDelta", "Continuing output.\n" * 30))
    QTest.qWait(150)
    assert not value(item, "followTail")


def test_wheel_reading_position_survives_stream_refresh_and_older_history(view):
    item, window = view
    rows = [record(f'history-{i}', f'h-{i}', content='History\n\n' * (4 + i % 3)) for i in range(30)]
    item.setProperty('messages', rows)
    QTest.qWait(250)
    before_wheel = value(item, 'contentY')
    wheel(window, 720)
    # Previously the wheel handler changed followTail without actually moving
    # the ListView, so a test of followTail alone could pass with broken scroll.
    assert value(item, 'contentY') < before_wheel - 20
    assert not value(item, 'followTail')
    name, offset = visible_anchor(item)
    for _ in range(10):
        invoke(item, 'appendDelta', delta('ModelTextDelta', 'Live output\n' * 5))
        item.setProperty('messages', list(rows))
        QTest.qWait(12)
    older = [record('earlier-' + str(i), 'earlier-r-' + str(i), content='Older history\n\n' * 7) for i in range(5)]
    item.setProperty('messages', older + rows)
    QTest.qWait(250)
    found = next(child for child in visual_items(item) if child.objectName() == name)
    assert abs(found.y() - value(item, 'contentY') - offset) < 3
    assert not value(item, 'followTail')
    invoke(item, 'jumpToTail')
    QTest.qWait(250)
    assert value(item, 'atYEnd') and value(item, 'followTail')


def test_streaming_row_promotes_in_place_preserving_open_reasoning(view):
    item, window = view
    invoke(item, 'appendDelta', delta('ModelReasoningDelta', 'Reasoning text.'))
    invoke(item, 'forceLayout')
    QTest.qWait(120)
    live = next(child for child in visual_items(item) if child.objectName() == 'streamingMessage')
    section = live.findChild(QQuickItem, 'reasoningSection')
    toggle = section.findChild(QQuickItem, 'reasoningToggle')
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    assert value(section, 'expanded')
    item.setProperty('messages', [record(content='Finished.', reasoning_content='Reasoning text.')])
    QTest.qWait(120)
    saved = message_item(item)
    assert saved is live
    assert value(section, 'expanded')
    assert not value(saved, 'streaming')


def test_scrollbar_drag_stops_following_and_preserves_reading_position(view):
    item, window = view
    rows = [record(f'row-{i}', f'r-{i}', content='History\n\n' * 7) for i in range(30)]
    item.setProperty('messages', rows)
    QTest.qWait(250)
    bar = item.findChild(QQuickItem, 'conversationScrollBar')
    start = bar.mapToScene(QPointF(bar.width()/2, (value(bar, 'position') + value(bar, 'size')/2) * bar.height())).toPoint()
    end = bar.mapToScene(QPointF(bar.width()/2, bar.height() * .35)).toPoint()
    QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(window, end, 60)
    QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, end)
    QTest.qWait(150)
    assert not value(item, 'followTail')
    name, offset = visible_anchor(item)
    invoke(item, 'appendDelta', delta('ModelTextDelta', 'New content\n' * 80))
    item.setProperty('messages', list(rows))
    QTest.qWait(150)
    found = next(child for child in visual_items(item) if child.objectName() == name)
    assert abs(found.y() - value(item, 'contentY') - offset) < 3


@pytest.mark.parametrize("name,field", [
    ("ask_user", "question"), ("update_progress", "notes"),
    ("record_check", "method"), ("create_ticket", "description"),
])
def test_other_prose_tool_arguments_render_readably(view, name, field):
    item, window = view
    prose = "## Result\n\n**Readable** output.\n\n- first item\n- second item"
    call = {"id": "prose", "function": {"name": name, "arguments": json.dumps({field: prose})}}
    item.setProperty("messages", [record(tool_calls=[call])])
    tool = tool_item(message_item(item))
    toggle = tool.findChild(QQuickItem, "toolToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    output = tool_field(tool, "toolProseOutput")
    wait_for(lambda: output.isVisible() and value(output, "sourceText") == prose)
    plain = text_document(output).toPlainText()
    assert "Readable output." in plain and "second item" in plain
    assert "##" not in plain and "**" not in plain and "- first" not in plain
    assert tool.findChild(QQuickItem, "toolDetails") is None


@pytest.mark.parametrize("name", ["run_command", "git_command"])
def test_command_and_result_readable_without_raw_envelope(view, name):
    item, window = view
    command = "python -m pytest -q" if name == "run_command" else "git status --short"
    call = {"id": "command", "function": {"name": name, "arguments": json.dumps({"command": command})}}
    output = "## literal output\n**still literal**\nfailed check"
    item.setProperty("messages", [record(tool_calls=[call]), record(
        "tool-result", role="tool", tool_call_id="command", content=json.dumps({
            "status": "ok", "data": {"exit_code": 1, "output": output, "cwd": "/worktree"},
        }),
    )])
    tool = tool_item(message_item(item))
    command_widget = tool.findChild(QQuickItem, "shellCommandText")
    assert command_widget.isVisible() and value(command_widget, "text") == command
    assert value(tool, "status") == "Failed"  # Nonzero exits cannot appear successful.
    assert tool.findChild(QQuickItem, "toolDetails") is None
    toggle = tool.findChild(QQuickItem, "toolToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    wait_for(lambda: tool_field(tool, "shellCommandOutput") is not None)
    rendered = tool_field(tool, "shellCommandOutput")
    wait_for(lambda: rendered.isVisible() and value(rendered, "text") == output)
    assert text_document(rendered).toPlainText() == output
    assert value(rendered, "readOnly") and value(rendered, "selectByMouse")
    assert tool.findChild(QQuickItem, "toolDetails") is None


def test_variable_height_tool_output_jump_stays_visually_stable(view):
    """An explicit Bottom jump must not bounce among estimated content heights."""
    conversation, _ = view
    conversation.setProperty("toolsExpandedByDefault", True)
    history = [
        record(f"mixed-{i}", f"mixed-request-{i}",
               content=("Short line.\n\n" * (2 + (i * 7) % 18)))
        for i in range(28)
    ]
    call = {"id": "large-output", "function": {
        "name": "extension_tool", "arguments": '{"query":"long text"}',
    }}
    history += [
        record("large-call", "large-request", content="Inspect result.", tool_calls=[call]),
        record("large-result", "large-request", role="tool",
               tool_call_id="large-output", content=json.dumps({
                   "status": "ok", "data": {
                       "log": "\n".join(f"line {i}: some detailed diagnostic text" for i in range(250)),
                   },
               })),
    ]
    conversation.setProperty("messages", history)
    QTest.qWait(250)
    invoke(conversation, "beginInteraction")
    conversation.setProperty("contentY", value(conversation, "originY"))
    invoke(conversation, "finishInteraction")
    assert not value(conversation, "followTail")

    invoke(conversation, "jumpToTail")
    QTest.qWait(250)
    samples = []
    for _ in range(20):
        QTest.qWait(20)
        samples.append(value(conversation, "contentY"))
    assert value(conversation, "followTail")
    assert value(conversation, "atYEnd")
    # A flash is a large backwards or forwards jump after the explicit jump
    # has settled, even when no new text is arriving.
    changes = [abs(b - a) for a, b in zip(samples, samples[1:])]
    assert max(changes, default=0) < 0.2 * value(conversation, "height")


def test_streaming_tail_does_not_repeatedly_jump_backwards(view):
    """Growing the final message must not provoke large reverse movements."""
    conversation, _ = view
    conversation.setProperty("messages", [
        record(f"history-{i}", f"r-{i}", content="History line.\n\n" * (4 + i % 8))
        for i in range(24)
    ])
    QTest.qWait(220)
    invoke(conversation, "beginInteraction")
    conversation.setProperty("contentY", value(conversation, "originY"))
    invoke(conversation, "finishInteraction")
    invoke(conversation, "jumpToTail")
    QTest.qWait(120)

    positions = []
    for i in range(45):
        invoke(conversation, "appendDelta", delta(
            "ModelTextDelta", f"Streaming line {i}.\n" * 4, request="growing-response"))
        QTest.qWait(16)
        positions.append(value(conversation, "contentY"))
    QTest.qWait(180)
    assert value(conversation, "followTail")
    assert value(conversation, "atYEnd")
    assert value(conversation, "contentY") > positions[0] + 150
    # No large reverse jumps while content is only growing.
    reverse_jumps = [a - b for a, b in zip(positions, positions[1:]) if b < a]
    assert max(reverse_jumps, default=0) < 0.2 * value(conversation, "height")
