"""Presentation-only navigation, tool discovery, and reasoning view state tests."""
import json

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from test_conversation_qml import (
    view, record, value, invoke, wait_for, message_item, visual_items,
)


def test_find_loaded_tool_result_without_conversation_request(view):
    conversation, _ = view
    tool_call = {"id": "tool-1", "function": {
        "name": "read_file", "arguments": '{"path":"src/module.py"}',
    }}
    history = [
        record("first", "r1", content="Inspecting file.", tool_calls=[tool_call]),
        record("result", "r1", role="tool", tool_call_id="tool-1",
               content=json.dumps({"status": "ok", "data": {"content": "UniqueNeedleInResult"}})),
        record("second", "r2", content="Continuing work."),
    ]
    conversation.setProperty("messages", history)
    QTest.qWait(100)
    conversation.setProperty("findTerm", "UniqueNeedleInResult")
    wait_for(lambda: value(conversation, "findMatches") == [0])
    invoke(conversation, "navigateFind", 1)
    assert value(conversation, "findCursor") == 0
    assert value(conversation, "highlightedIndex") == 0
    assert not value(conversation, "followTail")
    first = message_item(conversation, "first")
    assert value(first, "navigationHighlighted")
    matches = [item for item in visual_items(first)
               if item.objectName() == "toolToggle" and item.isVisible()]
    assert len(matches) == 1
    assert value(matches[0].parentItem(), "matchesFind")
    invoke(conversation, "navigateMessage", 1)
    assert value(conversation, "highlightedIndex") == 1
    invoke(conversation, "jumpToTail")
    assert value(conversation, "followTail")


def test_reasoning_user_choice_survives_message_virtualization(view):
    conversation, window = view
    conversation.setProperty("messages", [
        record("first", "first-request", content="Response", reasoning_content="Long reasoning."),
        *[record("later-" + str(i), "request-" + str(i), content="Many lines.\\n" * 14)
          for i in range(35)],
    ])
    QTest.qWait(140)
    invoke(conversation, "beginInteraction")
    conversation.setProperty("contentY", value(conversation, "originY"))
    invoke(conversation, "forceLayout")
    QTest.qWait(140)
    first = message_item(conversation, "first")
    toggle = first.findChild(QQuickItem, "reasoningToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     toggle.mapToScene(toggle.boundingRect().center()).toPoint())
    assert value(first.findChild(QQuickItem, "reasoningSection"), "expanded")
    invoke(conversation, "jumpToTail")
    QTest.qWait(180)
    invoke(conversation, "beginInteraction")
    conversation.setProperty("contentY", value(conversation, "originY"))
    invoke(conversation, "forceLayout")
    QTest.qWait(150)
    first_again = message_item(conversation, "first")
    assert value(first_again.findChild(QQuickItem, "reasoningSection"), "expanded")
