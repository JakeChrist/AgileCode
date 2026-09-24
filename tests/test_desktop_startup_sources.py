import importlib
import re
import sys
import types
from pathlib import Path


def _masked_qml(text: str) -> str:
    """Mask strings/comments while preserving braces and line numbers."""
    out: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char in ('"', "'"):
                state, quote = "string", char
                out.append(" ")
            elif char == "/" and next_char == "/":
                state = "line_comment"
                out.extend("  ")
                index += 1
            elif char == "/" and next_char == "*":
                state = "block_comment"
                out.extend("  ")
                index += 1
            else:
                out.append(char)
        elif state == "string":
            if char == "\\" and index + 1 < len(text):
                out.append(" ")
                index += 1
                out.append(" ")
            elif char == quote:
                state = "code"
                out.append(" ")
            else:
                out.append("\n" if char == "\n" else " ")
        elif state == "line_comment":
            if char == "\n":
                state = "code"
                out.append("\n")
            else:
                out.append(" ")
        else:
            if char == "*" and next_char == "/":
                state = "code"
                out.extend("  ")
                index += 1
            else:
                out.append("\n" if char == "\n" else " ")
        index += 1
    return "".join(out)


def _duplicate_bindings(path: Path) -> list[tuple[str, list[int]]]:
    """Find a QML property/signal binding assigned twice in one brace scope."""
    clean = _masked_qml(path.read_text())
    scopes: list[dict[str, list[int]]] = []
    finished: list[dict[str, list[int]]] = []
    token = re.compile(r"(?<![.A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*:|[{}]")
    for line_number, line in enumerate(clean.splitlines(), 1):
        for match in token.finditer(line):
            value = match.group(0)
            if value == "{":
                scopes.append({})
            elif value == "}":
                if scopes:
                    finished.append(scopes.pop())
            elif scopes:
                scopes[-1].setdefault(match.group(1), []).append(line_number)
    duplicates = []
    for scope in finished + scopes:
        for name, lines in scope.items():
            if len(lines) > 1:
                duplicates.append((name, lines))
    return duplicates


def test_qml_sources_have_no_duplicate_property_or_signal_bindings():
    qml_dir = Path("src/agilecode/desktop/qml")
    problems = {path.name: _duplicate_bindings(path) for path in qml_dir.glob("*.qml")}
    problems = {name: entries for name, entries in problems.items() if entries}
    assert not problems, problems


def test_qml_inline_object_assignments_do_not_use_illegal_separator():
    for path in Path("src/agilecode/desktop/qml").glob("*.qml"):
        qml = path.read_text()
        # `property: Type { ... }; onSignal:` is rejected by the QML parser.
        assert not re.search(r"}\s*;\s*on[A-Z][A-Za-z0-9_]*\s*:", qml), path


def test_engine_stop_is_safe_before_application_initializes(monkeypatch):
    # bridge.py deliberately keeps PySide6 at the presentation boundary. Stub the
    # tiny QtCore surface needed to import it so this startup-failure regression can
    # run in headless CI.
    pyside = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")

    class QObject:
        pass

    class Signal:
        def __init__(self, *args, **kwargs):
            pass

        def emit(self, *args, **kwargs):
            pass

    def Slot(*args, **kwargs):
        return lambda fn: fn

    class QTimer:
        def __init__(self, *args, **kwargs):
            pass

    qtcore.QObject = QObject
    qtcore.Signal = Signal
    qtcore.Slot = Slot
    qtcore.QTimer = QTimer
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    sys.modules.pop("agilecode.desktop.bridge", None)
    bridge = importlib.import_module("agilecode.desktop.bridge")

    class FakeLoop:
        def __init__(self):
            self.stopped = False

        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback):
            callback()

        def stop(self):
            self.stopped = True

    worker = bridge.EngineThread(Path("."), None, False, None)
    worker.loop = FakeLoop()
    worker.app = None
    worker.ready.clear()
    monkeypatch.setattr(worker, "join", lambda timeout=None: None)
    worker.stop()
    assert worker.stop_requested.is_set()
    assert worker.loop.stopped


def test_project_discussion_uses_separate_structured_chat_sessions():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    assert 'id: discussionView' in qml
    assert 'objectName: "projectDiscussionView"' in qml
    assert 'property var discussions: []' in qml
    assert 'property var selectedDiscussion: ({})' in qml
    assert 'property var discussionTranscript: []' in qml
    assert 'root.request("discussion_create", {})' in qml
    assert 'root.request("discussion_list"' in qml
    assert 'discussion: root.selectedDiscussion.id' in qml
    assert 'sessionId: root.selectedDiscussion.session_id || ""' in qml
    assert 'messages: root.discussionTranscript.filter' in qml
    assert 'source_discussion_id: root.pendingSourceDiscussion || null' in qml
    assert 'sessionId: "project-discussion"' not in qml
    assert 'property string discussionText' not in qml
    assert 'root.discussionText +=' not in qml
    assert 'TextArea { text: root.discussionText' not in qml
    assert '\\n\\nYOU\\n' not in qml
    assert '\\n\\nASSISTANT\\n' not in qml


def test_chat_interaction_controls_are_multiline_stable_and_stoppable():
    main = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    conversation = Path("src/agilecode/desktop/qml/ConversationView.qml").read_text()
    bridge = Path("src/agilecode/desktop/bridge.py").read_text()

    assert 'objectName: "discussionInput"' in main
    assert 'wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere' in main
    assert 'contentWidth: availableWidth' in main
    assert 'ScrollBar.horizontal.policy: ScrollBar.AlwaysOff' in main
    assert 'Layout.preferredHeight: Math.min(132, Math.max(42, discussionInput.contentHeight + 18))' in main
    assert 'event.modifiers & Qt.ShiftModifier' in main
    assert 'objectName: "discussionStop"' in main
    assert 'visible: root.discussionBusy' in main
    assert 'root.request("discussion_stop", {discussion: root.selectedDiscussion.id})' in main
    assert 'objectName: "ticketStopGeneration"' in main
    assert 'root.request("stop_generation", {ticket: root.selectedTicket})' in main

    assert 'recordId: "__streaming__"' in conversation
    assert 'footer: ChatMessage' not in conversation
    assert 'positionViewAtEnd' not in conversation
    assert 'contentY = bottom' in conversation

    assert 'def flush_deltas()' in bridge
    assert 'flush_deltas()' in bridge
