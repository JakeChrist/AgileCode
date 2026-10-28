"""GUI-only checks for conversation separation and compact tool presentation."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from test_conversation_qml import (
    qt, view, record, message_item, text_document, value, visual_items,
)
from test_tool_rendering_qml import (
    tool_view, populate, expand, tool_field,
)


def test_unattributed_user_role_is_neutral_but_authored_input_is_tinted(view):
    conversation, window = view
    conversation.setProperty("presentationScope", "/display/isolated-project")
    conversation.setProperty("locallyAuthoredInputIds", {
        '["/display/isolated-project","session","written-id"]': True,
    })
    conversation.setProperty("messages", [
        record("auto-instruction", "engine-id", role="user",
               content="Perform the assigned step."),
        record("human", "written-id", role="user",
               content="Please review the patch."),
        record("agent", "agent-response", content="The change is ready.",
               reasoning_content="I should compare the files before answering."),
    ])
    auto = message_item(conversation, "auto-instruction")
    human = message_item(conversation, "human")
    agent = message_item(conversation, "agent")
    assert not value(auto, "locallyAuthored")
    assert value(human, "locallyAuthored")
    auto_surface = auto.findChild(QQuickItem, "messageSurface")
    human_surface = human.findChild(QQuickItem, "messageSurface")
    agent_surface = agent.findChild(QQuickItem, "messageSurface")
    assert value(auto_surface, "color") == value(agent_surface, "color")
    assert value(human_surface, "color") != value(agent_surface, "color")

    for row in (auto, human, agent):
        # No "You", "Assistant", "Project Manager", or "Response" author labels.
        assert not row.findChild(QQuickItem, "messageRole").isVisible()
        assert row.findChild(QQuickItem, "responseLabel") is None
    assert text_document(agent.findChild(QQuickItem, "answerText")).toPlainText() == "The change is ready."

    reasoning = agent.findChild(QQuickItem, "reasoningSection")
    panel = reasoning.findChild(QQuickItem, "reasoningSurface")
    assert panel is not None and not panel.isVisible()
    toggle = reasoning.findChild(QQuickItem, "reasoningToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    QTest.qWait(25)
    assert panel.isVisible()
    text = reasoning.findChild(QQuickItem, "reasoningText")
    assert text_document(text).toPlainText() == "I should compare the files before answering."


def test_legacy_ticket_feedback_marked_as_human_not_agent_instruction(view):
    conversation, _ = view
    conversation.setProperty("ticketConversation", True)
    conversation.setProperty("messages", [
        record("step-instruction", "engine", role="user",
               content="Perform the assigned step."),
        record("feedback", "human", role="user",
               content="USER FEEDBACK: Fix the implementation."),
    ])
    instruction = message_item(conversation, "step-instruction")
    feedback = message_item(conversation, "feedback")
    assert not value(instruction, "locallyAuthored")
    assert value(feedback, "locallyAuthored")
    assert value(instruction.findChild(QQuickItem, "messageSurface"), "color") != value(
        feedback.findChild(QQuickItem, "messageSurface"), "color")


def test_edit_file_has_one_original_and_one_replacement_label(tool_view):
    root, tool, window = tool_view
    before = "bool ready() {\\n    return false;\\n}"
    after = "bool ready() {\\n    return true;\\n}"
    populate(tool, "edit_file", {
        "path": "src/net.cpp",
        "expected_hash": "abc123",
        "replacements": [{"old": before, "new": after}],
    }, {"status": "ok", "data": {"path": "src/net.cpp", "hash": "def456"}})
    expand(root, tool, window)

    old_text = tool_field(tool, "toolValue-inputs/replacements/0/old")
    new_text = tool_field(tool, "toolValue-inputs/replacements/0/new")
    assert old_text is not None and new_text is not None
    assert text_document(old_text).toPlainText() == before
    assert text_document(new_text).toPlainText() == after

    panels = [item for item in visual_items(tool)
              if item.objectName() == "completeToolField" and item.isVisible()]
    headings = [value(item, "title") for item in panels]
    assert headings.count("Original text") == 1
    assert headings.count("Replacement text") == 1

    for field_name in ("old", "new"):
        duplicate = tool_field(tool, "toolLabel-inputs/replacements/0/" + field_name)
        assert duplicate is not None and not duplicate.isVisible()

    # Each short code block presents one actions menu rather than three buttons.
    # Copy, wrapping and full inspection remain available inside the menu.
    visible_buttons = [
        value(item, "text") for panel in panels for item in visual_items(panel)
        if item.isVisible() and value(item, "text") in ("⋯", "Copy", "Open full", "Wrap", "Unwrap")
    ]
    assert visible_buttons == ["⋯", "⋯"]
