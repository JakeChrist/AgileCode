import importlib
import ast
import inspect
import json
import re
import sqlite3
import sys
import textwrap
import types
from pathlib import Path

from agilecode.domain.types import LEGACY_ALLOWED_TOOL_GROUPS, Settings


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
    # Do not replace the real bridge module (or its parent-package attribute):
    # real Qt tests may already have imported it, or may import it after this.
    spec = importlib.util.spec_from_file_location(
        "agilecode.desktop._startup_bridge_test", Path("src/agilecode/desktop/bridge.py"))
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

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
    # Opening a project may own migration/file work; stopping the event loop
    # midway would discard that work. run() observes the stop request afterward.
    assert not worker.loop.stopped


def test_project_discussion_uses_separate_structured_chat_sessions():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    assert 'id: discussionView' in qml
    assert 'objectName: "projectDiscussionView"' in qml
    assert 'property var discussions: []' in qml
    assert 'property var selectedDiscussion: ({})' in qml
    assert 'property var discussionTranscript: []' in qml
    assert 'root.request("discussion_create", {})' in qml
    assert 'request("discussion_list"' in qml
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


def test_tool_architecture_uses_tools_and_one_manager():
    from inspect import getsourcefile
    from agilecode.core.tools import Tool
    tools = Path(getsourcefile(Tool)).read_text()
    manager = Path("src/agilecode/desktop/qml/TeamManager.qml").read_text()

    assert "class Tool:" in tools
    assert "class ToolManager:" in tools
    assert "class RunCommandTool(Tool):" in tools
    assert "class CommandPolicy:" in tools
    assert "class MCPTool(Tool):" in tools
    assert "class ToolRegistry" not in tools
    assert "class ToolExecutor" not in tools
    assert "class ToolSpec" not in tools
    assert "_build_registry" not in tools
    assert "step_kinds" not in tools
    assert "development_only" not in tools
    assert 'text: "Allowed tools"' in manager
    assert LEGACY_ALLOWED_TOOL_GROUPS["read"] == ("list_files", "read_file", "read_job_output", "read_artifact")


def test_team_management_uses_structured_gui_not_json_editor():
    main = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    manager = Path("src/agilecode/desktop/qml/TeamManager.qml").read_text()

    assert "TeamManager {" in main
    assert "teamJson" not in main
    assert "JSON.parse(teamJson.text)" not in main
    assert 'text: "Team settings"' in manager
    assert 'text: "MEMBERS"' in manager
    assert 'text: "PROCESSES"' in manager
    assert 'text: "STEPS"' in manager
    assert 'text: "System prompt"' in manager
    assert 'text: "Assigned member"' in manager
    assert 'text: "Correction target"' in manager
    assert 'function addMember()' in manager
    assert 'function addProcess()' in manager
    assert 'function addStep()' in manager
    assert 'function moveStep(delta)' in manager
    assert 'manager.saveRequested(manager.copy(manager.draft)' in manager


def test_ticket_inspector_contains_only_ticket_summary_tabs():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()

    assert 'model: ["Overview", "Steps", "Conversation"]' in qml
    assert 'model: ["Overview", "Steps", "Conversation", "Files", "Jobs", "Merge"]' not in qml
    assert 'id: sourcePath' not in qml
    assert 'id: jobOutput' not in qml
    assert 'text: "Prepare / rebuild"' not in qml


async def test_ticket_conversation_opens_at_tail_and_pages_backward():
    from agilecode.application.query_views import ProjectQueries

    main = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    # Exercise the shared query rather than requiring SQL in the old facade.
    class Store:
        async def query(self, query, params):
            return [dict(row) for row in db.execute(query, params)]

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE messages (id TEXT, session_id TEXT, seq INTEGER, body TEXT)")
        db.executemany("INSERT INTO messages VALUES (?,?,?,?)", [
            (str(seq), session, seq, json.dumps({"role": "assistant", "content": str(seq)}))
            for session, seq in [("session", 1), ("session", 2), ("session", 3), ("session", 4), ("other", 5)]
        ])
        query = ProjectQueries(Store(), lambda: 2)
        assert [row["seq"] for row in await query.conversation("session")] == [3, 4]
        assert [row["seq"] for row in await query.conversation("session", before=3)] == [1, 2]
        assert [row["seq"] for row in await query.conversation("session", after=2)] == [3, 4]
    finally:
        db.close()
    assert 'before: null' in main
    assert 'Load 200 older messages' in main
    assert 'Load next 200 messages' not in main
    assert 'conversationAfter' not in main


def test_ticket_conversation_supports_correction_bottom_and_step_following():
    from agilecode.application.execution import ExecutionOperations

    main = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    conversation = Path("src/agilecode/desktop/qml/ConversationView.qml").read_text()

    assert 'objectName: "ticketCorrectionInput"' in main
    assert 'objectName: "ticketCorrectionSend"' in main
    assert 'text: conversationActive ? "Interrupt & correct" : "Send correction"' in main
    assert 'root.submitConversation("feedback", {ticket: root.selectedTicket, interrupt: true}' in main
    assert 'objectName: "conversationBottom"' in main
    assert 'Exact prompts / requests' not in main
    assert 'onClicked: conversationView.jumpToTail()' in main
    assert 'function jumpToTail()' in conversation
    assert 'event.kind === "StepAttemptStarted"' in main
    assert 'root.selectSession(event.session_id)' in main
    assert 'inspector.opened' in main
    feedback = ast.parse(textwrap.dedent(inspect.getsource(ExecutionOperations.submit_feedback)))
    acceptance = next(node for node in ast.walk(feedback) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name) and node.func.id == "accept_ticket_feedback")
    expected = ['self.store', 'ticket["id"]', 'run["id"]', 'attempt["session_id"]', 'text', 'input_id']
    assert [ast.dump(argument) for argument in acceptance.args] == [
        ast.dump(ast.parse(expression, mode="eval").body) for expression in expected]


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
    # Wheel/scrollbar interaction and stable tail positioning are exercised in
    # test_conversation_qml, rather than prescribing a particular QML method.

    assert 'def flush_deltas()' in bridge
    assert 'flush_deltas()' in bridge


def test_settings_ui_has_no_output_token_limit():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    assert 'id: outputField' not in qml
    assert 'profile.max_tokens' not in qml
    assert 'Context / output tokens' not in qml
    assert 'id: contextField' not in qml
    assert 'profile.context_window' not in qml
    assert 'Context window' not in qml


def test_reasoning_default_is_a_display_setting():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    conversation = Path("src/agilecode/desktop/qml/ConversationView.qml").read_text()

    assert Settings().open_reasoning_by_default is False
    assert 'id: reasoningOpenSwitch' in qml
    assert 'text: "Open reasoning blocks by default"' in qml
    assert 'updated.open_reasoning_by_default = reasoningOpenSwitch.checked' in qml
    assert 'reasoningExpandedByDefault: !!root.settings.open_reasoning_by_default' in qml
    assert 'property bool reasoningExpandedByDefault: false' in conversation


def test_agreed_execution_limits_are_visible_in_settings():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    from inspect import getsourcefile
    from agilecode.core.tools import Tool
    tools = Path(getsourcefile(Tool)).read_text()
    runner = Path("src/agilecode/core/runner.py").read_text()

    assert 'id: providerTimeoutField' in qml
    assert 'id: shellTimeoutField' in qml
    assert 'id: correctionLimitField' in qml
    assert 'Blank = no timeout' in qml
    assert 'max_requests_per_attempt' not in Settings.model_fields
    assert 'max_requests_per_attempt' not in runner
    assert Settings().max_corrections == 10
    assert Settings(max_corrections=None).max_corrections is None
    assert Settings().shell_command_timeout_seconds is None
    assert 'Blank = unlimited' in qml
    assert 'class DelegateArgs' not in tools
    assert '"delegate"' not in tools


def test_discussion_create_ticket_prefills_existing_dialog_from_generated_draft():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    bridge = Path("src/agilecode/desktop/bridge.py").read_text()

    assert 'property string pendingDraftTitle: ""' in qml
    assert 'property string pendingDraftDescription: ""' in qml
    assert 'property bool ticketDraftBusy: false' in qml
    assert 'root.request("discussion_ticket_draft", {discussion: root.selectedDiscussion.id})' in qml
    assert 'text: root.ticketDraftBusy ? "Drafting ticket…" : "Create ticket"' in qml
    assert 'root.prepareNewTicket(data.discussion.id, data.discussion.title, data.title, data.description)' in qml
    assert 'newTitle.text = root.pendingDraftTitle' in qml
    assert 'newDescription.text = root.pendingDraftDescription' in qml
    assert 'if action=="discussion_ticket_draft":return await app.discussion.draft_ticket(d["discussion"])' in bridge


def test_generated_ticket_descriptions_request_and_preserve_markdown():
    discussion = Path("src/agilecode/core/discussion.py").read_text()
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()

    assert 'readable Markdown' in discussion
    assert 'headings' in discussion
    assert 'Markdown lists' in discussion
    assert 'dense prose' in discussion
    assert 'do not use Markdown' not in discussion
    assert 'pendingDraftDescription = draftDescription || ""' in qml
    assert 'pendingDraftDescription = markdownPresentation.plainText' not in qml
    assert 'editDescription.text = root.detail.ticket.description' in qml
    assert 'editDescription.text = markdownPresentation.plainText' not in qml
    assert 'TextArea { id: newDescription; selectByMouse: true; textFormat: TextEdit.PlainText' in qml
    assert 'TextArea { id: editDescription; color: root.ink; textFormat: TextEdit.PlainText' in qml


def test_ticket_description_uses_compact_preview_and_full_reading_dialog():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    assert 'property bool ticketDescriptionExpanded' not in qml
    assert 'objectName: "ticketDescriptionToggle"' not in qml
    assert 'objectName: "ticketDescriptionPreviewCard"' in qml
    assert 'objectName: "ticketDescriptionPreview"' in qml
    assert 'Layout.preferredHeight: 118' in qml
    assert 'textFormat: Text.MarkdownText' in qml
    assert 'maximumLineCount: 6' in qml
    assert 'objectName: "ticketDescriptionOpen"' in qml
    assert 'text: "View full description"' in qml
    assert 'onClicked: ticketDescriptionDialog.open()' in qml
    assert 'objectName: "ticketDescriptionDialog"' in qml
    assert 'width: Math.min(root.width - 120, 980)' in qml
    assert 'height: Math.min(root.height - 120, 760)' in qml
    assert 'objectName: "ticketDescriptionDialogScroll"' in qml
    assert 'objectName: "ticketDescriptionDialogText"' in qml
    assert 'textFormat: TextEdit.MarkdownText' in qml
    assert 'readOnly: true' in qml
    assert 'selectByMouse: true' in qml
    assert 'objectName: "ticketDescriptionScroll"' not in qml


def test_all_model_prose_uses_rendered_presentation_paths():
    qml = Path("src/agilecode/desktop/qml")
    chat = (qml / "ChatMessage.qml").read_text()
    reasoning = (qml / "ReasoningSection.qml").read_text()
    tool = (qml / "ToolCall.qml").read_text()
    rendered = (qml / "RenderedText.qml").read_text()
    main = (qml / "Main.qml").read_text()
    launcher = Path("src/agilecode/desktop/launcher.py").read_text()

    assert "RenderedText {" in chat
    assert "RenderedText {" in reasoning
    assert 'textFormat: TextEdit.PlainText' not in reasoning
    assert 'proseControl: ["finish_step", "ask_user", "update_progress", "record_check", "create_ticket"]' in tool
    assert '"delegate"' not in tool
    assert 'objectName: "toolProseSummary"' in tool
    assert 'objectName: "toolProseOutput"' in tool

    assert 'textFormat: (streaming || partial) ? TextEdit.PlainText : TextEdit.MarkdownText' in rendered
    assert 'text = text.replace(/\\*\\*/g, "");' in rendered
    assert 'text = text.replace(/^\\s*#+\\s+/gm, "");' in rendered

    assert 'MarkdownPresentation { id: markdownPresentation }' in main
    assert 'pendingDraftDescription = draftDescription || ""' in main
    assert 'pendingDraftDescription = markdownPresentation.plainText' not in main
    assert 'editDescription.text = root.detail.ticket.description' in main
    assert 'editDescription.text = markdownPresentation.plainText' not in main
    assert 'markdownPresentation.plainText(modelData.result.summary)' in main
    assert 'questionText.sourceText = modelData.prompt' in main

    assert 'qmlRegisterType(MarkdownPresentation' in launcher


def test_ticket_resume_is_one_ticket_level_control():
    qml = Path("src/agilecode/desktop/qml/Main.qml").read_text()
    bridge = Path("src/agilecode/desktop/bridge.py").read_text()

    assert 'objectName: "ticketResumeExecution"' in qml
    assert 'text: "Resume"' in qml
    assert 'root.request("resume_execution", {ticket: root.selectedTicket})' in qml

    assert 'ticketExecutionRecovery' not in qml
    assert 'ticketResetCurrentStep' not in qml
    assert 'restart_current_step' not in qml
    assert 'attemptContinueButton' not in qml
    assert 'attemptResetButton' not in qml
    assert 'conversationContinueInterrupted' not in qml
    assert 'canContinueInterruptedStep' not in qml

    assert 'if action in {"resume","resume_execution"}' in bridge
    assert 'restart_current_step' not in bridge
    assert 'reset_step' not in bridge
