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


def test_speaker_surfaces_and_reasoning_are_distinct_without_changing_text(view):
    conversation, window = view
    conversation.setProperty("messages", [
        record("user", "user-request", role="user", content="Please review the patch."),
        record("agent", "agent-response", content="The change is ready.",
               reasoning_content="I should compare the files before answering."),
    ])
    user_message = message_item(conversation, "user")
    assistant_message = message_item(conversation, "agent")
    user_surface = user_message.findChild(QQuickItem, "messageSurface")
    assistant_surface = assistant_message.findChild(QQuickItem, "messageSurface")
    assert user_surface is not None and assistant_surface is not None
    assert user_surface.property("color") != assistant_surface.property("color")

    label = assistant_message.findChild(QQuickItem, "responseLabel")
    assert label.isVisible() and value(label, "text") == "Response"
    answer = assistant_message.findChild(QQuickItem, "answerText")
    assert text_document(answer).toPlainText() == "The change is ready."

    reasoning = assistant_message.findChild(QQuickItem, "reasoningSection")
    panel = reasoning.findChild(QQuickItem, "reasoningSurface")
    assert panel is not None and not panel.isVisible()
    toggle = reasoning.findChild(QQuickItem, "reasoningToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    QTest.qWait(25)
    assert panel.isVisible()
    text = reasoning.findChild(QQuickItem, "reasoningText")
    assert text_document(text).toPlainText() == "I should compare the files before answering."


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
