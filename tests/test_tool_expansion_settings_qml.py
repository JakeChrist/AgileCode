"""Exercise saved tool-disclosure preferences through real Qt and configuration IO."""
import json

import pytest

from agilecode.adapters.config import Configuration
from agilecode.adapters.store import ProjectStore
from agilecode.core.types import Settings
from test_conversation_qml import (
    QObject, QPointF, QQuickItem, QTest, invoke, message_item,
    qt, record, tool_item, value, view, visible_anchor, visual_items, wait_for,
)
from test_message_submission_qml import role_ui, ui
from test_tool_settings_qml import click, send_snapshot


def call(identity="call", command="printf 'hello'"):
    return {"id": identity, "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}


def result(identity="result", request="r1", call_id="call", output="hello\n"):
    return {"id": identity, "seq": 2, "request_id": request,
            "body": {"role": "tool", "tool_call_id": call_id,
                     "content": json.dumps({"status": "ok", "data": {"output": output, "exit_code": 0}})}}


def reveal_and_click(window, item):
    """Scroll the real Settings viewport to the target before the pointer click."""
    parent = item.parentItem()
    while parent is not None:
        if parent.metaObject().indexOfProperty("contentY") >= 0:
            content = value(parent, "contentItem")
            top = item.mapToItem(content, QPointF(0, 0)).y()
            parent.setProperty("contentY", max(0, min(top - parent.height() / 2,
                               value(parent, "contentHeight") - parent.height())))
            QTest.qWait(30)
        parent = parent.parentItem()
    click(window, item)


async def config_snapshot(backend, config):
    snapshots = await config.snapshots()
    backend.response.emit("refresh", "snapshot", json.dumps({"ok": True, "data": {
        "board": [], "teams": [], "skills": [], "settings": snapshots["effective"],
        "settings_snapshots": snapshots,
    }}))


def test_legacy_settings_keep_tools_collapsed_by_default():
    settings = Settings.model_validate({"open_reasoning_by_default": True, "theme": "light"})
    assert settings.open_tools_by_default is False
    assert settings.open_reasoning_by_default is True
    assert Settings.model_validate_json(settings.model_dump_json()).open_tools_by_default is False


@pytest.mark.parametrize("scope,index", [("project", 0), ("global", 1)])
@pytest.mark.asyncio
async def test_switch_save_boundary_and_config_reload(ui, tmp_path, scope, index):
    root, backend = ui
    store = ProjectStore(tmp_path / "project" / "db")
    await store.open()
    config = Configuration(tmp_path / "global", store)
    try:
        await config.load()
        await config.patch_settings({"open_reasoning_by_default": True}, "global")
        await config_snapshot(backend, config)
        root.setProperty("currentPage", 5)
        root.setProperty("pmPanelExpanded", False)
        control = root.findChild(QQuickItem, "toolsOpenCheckBox")
        assert control.inherits("QQuickSwitch")
        assert value(control, "text") == "Open tool blocks by default: Off"
        assert value(root, "settings")["open_reasoning_by_default"] is True
        reveal_and_click(root, control)
        assert value(control, "text") == "Open tool blocks by default: On (unsaved)"
        assert value(root, "settings")["open_tools_by_default"] is False
        # A background refresh must neither apply nor discard the unsaved choice.
        await config_snapshot(backend, config)
        assert value(control, "checked") and value(control, "modified")
        invoke(root, "saveToolSettings")
        assert backend.latest("settings")[2]["settings"]["open_tools_by_default"] is False
        root.findChild(QObject, "settingsScope").setProperty("currentIndex", index)
        reveal_and_click(root, root.findChild(QQuickItem, "saveBasicSettingsButton"))
        command = backend.latest("settings")
        assert command[2]["scope"] == scope
        assert command[2]["settings"]["open_tools_by_default"] is True
        assert command[2]["settings"]["open_reasoning_by_default"] is True
        await config.save(Settings.model_validate(command[2]["settings"]), scope,
                          expected=command[2]["expected"])
        backend.respond(command, ok=True, data=config.settings.model_dump(mode="json"))
        # Recreate the configuration reader, as happens on restart, then reload UI.
        reloaded = Configuration(tmp_path / "global", store)
        assert (await reloaded.load()).open_tools_by_default is True
        await config_snapshot(backend, reloaded)
        assert value(control, "text") == "Open tool blocks by default: On"
        assert value(root, "settings")["open_tools_by_default"] is True
        # The explicit Off choice is persisted through the same save path.
        reveal_and_click(root, control)
        assert value(control, "text") == "Open tool blocks by default: Off (unsaved)"
        reveal_and_click(root, root.findChild(QQuickItem, "saveBasicSettingsButton"))
        command = backend.latest("settings")
        await reloaded.save(Settings.model_validate(command[2]["settings"]), scope,
                            expected=command[2]["expected"])
        backend.respond(command, ok=True, data=reloaded.settings.model_dump(mode="json"))
        final = Configuration(tmp_path / "global", store)
        assert (await final.load()).open_tools_by_default is False
        assert final.settings.open_reasoning_by_default is True
        await config_snapshot(backend, final)
        assert value(control, "text") == "Open tool blocks by default: Off"
    finally:
        await store.close()


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_saved_setting_updates_existing_incoming_and_orphan_blocks(ui, role):
    root, backend = ui
    send_snapshot(backend, Settings().model_dump(mode="json"))
    _, conversation, _, _ = role_ui(root, role)
    history_name = {"pm_message": "pmTranscript", "feedback": "transcript", "discuss": "discussionTranscript"}[role]
    history = [record(tool_calls=[call()]), result(), result("orphan", "orphan-request", "unloaded")]
    root.setProperty(history_name, history)
    paired = tool_item(message_item(conversation))
    orphan = tool_item(message_item(conversation, "orphan"))
    assert not value(paired, "expanded") and not value(orphan, "expanded")
    send_snapshot(backend, Settings(open_tools_by_default=True).model_dump(mode="json"), "on")
    wait_for(lambda: value(paired, "expanded") and value(orphan, "expanded"))
    assert value(conversation, "toolsExpandedByDefault") is True
    assert value(conversation, "followTail") is True
    # Every new saved block uses the currently saved setting, in every surface.
    history += [record("incoming", "new-request", tool_calls=[call("incoming")])]
    root.setProperty(history_name, history)
    incoming = tool_item(message_item(conversation, "incoming"))
    assert value(incoming, "expanded")
    send_snapshot(backend, Settings(open_tools_by_default=False).model_dump(mode="json"), "off")
    wait_for(lambda: not value(paired, "expanded") and not value(orphan, "expanded")
             and not value(incoming, "expanded"))


@pytest.mark.parametrize("role", ["pm_message", "feedback", "discuss"])
def test_live_manual_collapse_survives_deltas_and_saved_result(ui, role):
    root, backend = ui
    send_snapshot(backend, Settings(open_tools_by_default=True).model_dump(mode="json"))
    _, conversation, session, _ = role_ui(root, role)
    history_name = {"pm_message": "pmTranscript", "feedback": "transcript", "discuss": "discussionTranscript"}[role]
    prefix = {"pm_message": "ProjectManager", "feedback": "Model", "discuss": "Discussion"}[role]
    event_kind = "ModelToolDelta" if role == "feedback" else prefix + "Delta"
    def emit(fragment):
        backend.event.emit(json.dumps({"kind": event_kind, "session_id": session, "data": {
            "kind": "tool_delta", "request_id": "live-request", "turn_id": "turn", "value": fragment}}))
    emit({"index": 0, **call("live", "one")})
    invoke(conversation, "forceLayout")
    wait_for(lambda: any(child.objectName() == "streamingMessage" for child in visual_items(conversation)))
    live = next(child for child in visual_items(conversation) if child.objectName() == "streamingMessage")
    tool = tool_item(live)
    assert value(tool, "expanded")
    click(root, tool.findChild(QQuickItem, "toolToggle"))
    assert not value(tool, "expanded")
    emit({"index": 0, "function": {"arguments": " "}})
    QTest.qWait(150)
    assert not value(tool_item(live), "expanded")
    history = [record("saved", "live-request", tool_calls=[call("live", "one")]),
               result("saved-result", "live-request", "live")]
    root.setProperty(history_name, history)
    saved_tool = tool_item(message_item(conversation, "saved"))
    assert not value(saved_tool, "expanded")
    assert value(saved_tool, "resultBody") is not None
    # Unchanged snapshots keep that individual override; flipping Off then On resets it.
    send_snapshot(backend, Settings(open_tools_by_default=True).model_dump(mode="json"), "unchanged")
    assert not value(saved_tool, "expanded")
    send_snapshot(backend, Settings(open_tools_by_default=False).model_dump(mode="json"), "off")
    send_snapshot(backend, Settings(open_tools_by_default=True).model_dump(mode="json"), "on")
    wait_for(lambda: value(saved_tool, "expanded"))


def test_bulk_preference_change_preserves_reading_anchor_and_tail_follow(view):
    conversation, _ = view
    rows = [record(f"row-{i}", f"request-{i}", content="History paragraph.\n\n" * 5,
                   tool_calls=[call(f"tool-{i}")]) for i in range(35)]
    conversation.setProperty("messages", rows)
    QTest.qWait(200)
    invoke(conversation, "beginInteraction")
    conversation.setProperty("contentY", value(conversation, "contentY") - 1800)
    invoke(conversation, "finishInteraction")
    QTest.qWait(150)
    name, offset = visible_anchor(conversation)
    for enabled in (True, False, True):
        conversation.setProperty("toolsExpandedByDefault", enabled)
        QTest.qWait(200)
        anchor = next(child for child in visual_items(conversation) if child.objectName() == name)
        assert abs(anchor.y() - value(conversation, "contentY") - offset) < 3
        assert not value(conversation, "followTail")
    invoke(conversation, "jumpToTail")
    conversation.setProperty("toolsExpandedByDefault", False)
    QTest.qWait(200)
    assert value(conversation, "followTail") and value(conversation, "atYEnd")


def test_late_streamed_tool_id_keeps_manual_override(view):
    conversation, window = view
    conversation.setProperty("toolsExpandedByDefault", True)
    invoke(conversation, "appendDelta", {"kind": "ModelToolDelta", "session_id": "session", "data": {
        "request_id": "late-id", "value": {"index": 0, "function": {"name": "run_command", "arguments": '{"command": "'}}}})
    invoke(conversation, "forceLayout")
    wait_for(lambda: any(child.objectName() == "streamingMessage" for child in visual_items(conversation)))
    live = next(child for child in visual_items(conversation) if child.objectName() == "streamingMessage")
    tool = tool_item(live)
    assert value(tool, "expanded")
    click(window, tool.findChild(QQuickItem, "toolToggle"))
    assert not value(tool, "expanded")
    invoke(conversation, "appendDelta", {"kind": "ModelToolDelta", "session_id": "session", "data": {
        "request_id": "late-id", "value": {"index": 0, "id": "assigned-later", "function": {"arguments": 'echo hello"}'}}}})
    QTest.qWait(150)
    updated = tool_item(live)
    assert value(updated, "callData")["id"] == "assigned-later"
    assert not value(updated, "expanded")
    conversation.setProperty("messages", [record("persisted-late", "late-id", tool_calls=[call("assigned-later")])])
    assert not value(tool_item(message_item(conversation, "persisted-late")), "expanded")


def test_individual_override_survives_virtualization_without_affecting_other_calls(view):
    conversation, window = view
    conversation.setProperty("toolsExpandedByDefault", True)
    history = [record("first", "first-request", tool_calls=[call("one"), call("two")])]
    history += [record(f"history-{i}", f"history-request-{i}", content="Saved history.\n\n" * 8) for i in range(40)]
    conversation.setProperty("messages", history)
    conversation.setProperty("currentIndex", -1)  # The current item is deliberately retained by ListView.
    QTest.qWait(150)
    invoke(conversation, "beginInteraction")
    conversation.setProperty("contentY", value(conversation, "originY"))
    invoke(conversation, "forceLayout")
    QTest.qWait(150)
    first = message_item(conversation, "first")
    tools = [item.parentItem() for item in visual_items(first)
             if item.objectName() == "toolToggle" and item.parentItem().isVisible()]
    assert len(tools) == 2 and all(value(tool, "expanded") for tool in tools)
    click(window, tools[0].findChild(QQuickItem, "toolToggle"))
    assert not value(tools[0], "expanded") and value(tools[1], "expanded")
    invoke(conversation, "jumpToTail")
    QTest.qWait(180)
    assert not any(item.objectName() == "chatMessage-first" for item in visual_items(conversation))
    invoke(conversation, "beginInteraction")
    conversation.setProperty("contentY", value(conversation, "originY"))
    invoke(conversation, "forceLayout")
    QTest.qWait(180)
    recreated = message_item(conversation, "first")
    tools = [item.parentItem() for item in visual_items(recreated)
             if item.objectName() == "toolToggle" and item.parentItem().isVisible()]
    assert not value(tools[0], "expanded") and value(tools[1], "expanded")


@pytest.mark.parametrize("hidden_during_save", [False, True])
def test_bulk_expansion_preserves_visible_tool_header_inside_long_message(view, hidden_during_save):
    conversation, window = view
    calls = [call(f"call-{i}") for i in range(12)]
    history = [record(tool_calls=calls)]
    history += [result(f"result-{i}", call_id=f"call-{i}", output="Output line.\n" * 15) for i in range(12)]
    conversation.setProperty("messages", history)
    message = message_item(conversation)
    def selected_tool():
        return next(item.parentItem() for item in visual_items(message)
                    if item.objectName() == "toolToggle"
                    and value(item.parentItem(), "callData").get("id") == "call-6")
    target = selected_tool()
    invoke(conversation, "beginInteraction")
    top = message.y() + target.mapToItem(message, QPointF(0, 0)).y()
    conversation.setProperty("contentY", top - 20)
    invoke(conversation, "finishInteraction")
    QTest.qWait(100)
    before = target.mapToScene(QPointF(0, 0)).y()
    assert abs(before - 20) < 3
    for enabled in (True, False, True):
        if hidden_during_save:
            conversation.setProperty("visible", False)
        conversation.setProperty("toolsExpandedByDefault", enabled)
        QTest.qWait(100)
        conversation.setProperty("visible", True)
        QTest.qWait(200)
        assert abs(selected_tool().mapToScene(QPointF(0, 0)).y() - before) < 3, (value(conversation, "readingAnchor"), value(conversation, "anchorNeedsRestore"), value(conversation, "contentY"))
        assert not value(conversation, "followTail")
