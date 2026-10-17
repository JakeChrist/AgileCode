"""Provider timeout copy and explicit edit/save boundaries through the real QML UI."""
import json

import pytest

from test_tool_settings_qml import (
    QObject, QQuickItem, Qt, QTest, click, invoke, main, provider, qt, value,
    visual_item, wait_for,
)


def open_provider(root, backend, timeout=180.0):
    original = provider("local")
    original["profile"].update(timeout_seconds=timeout,
                               request_options={"temperature": 0.2},
                               round_trip_reasoning=True)
    backend.response.emit("refresh", "providers", json.dumps({"ok": True, "data": {
        "format_version": 1, "default_provider_id": "local", "providers": [original],
    }}))
    root.setProperty("currentPage", 4)
    dialog = root.findChild(QObject, "providerDialog")
    # Open the JSON-backed row: direct QVariant conversion would erase nulls.
    wait_for(lambda: visual_item(root, "providerEdit-local") is not None)
    click(root, visual_item(root, "providerEdit-local"))
    wait_for(lambda: value(dialog, "opened"))
    return dialog, original, root.findChild(QQuickItem, "providerTimeoutField")


@pytest.mark.parametrize("timeout,text", [(None, ""), (180.0, "180")])
def test_provider_timeout_copy_and_unchanged_save(main, timeout, text):
    root, backend = main
    _, original, field = open_provider(root, backend, timeout)
    label = root.findChild(QQuickItem, "providerTimeoutLabel")
    help_text = root.findChild(QQuickItem, "providerTimeoutHelp")
    assert value(label, "text") == "Connection/setup timeout (seconds)"
    assert "connections, request uploads, connection-pool waits, and metadata reads" in value(help_text, "text")
    assert "Model generation and streaming have no read timeout." in value(help_text, "text")
    assert value(field, "placeholderText") == "Blank = no setup timeout"
    assert value(field, "text") == text
    click(root, root.findChild(QQuickItem, "providerSave"))
    action, payload = backend.calls[-1]
    assert action == "save_provider"
    assert payload["provider"] == original
    assert payload["expected"] == original


@pytest.mark.parametrize("text,timeout", [("", None), ("42.5", 42.5)])
def test_provider_timeout_changes_only_on_explicit_save(main, text, timeout):
    root, backend = main
    _, original, field = open_provider(root, backend)
    field.setProperty("text", text)
    assert value(root, "providerPool")["providers"] == [original]
    assert not any(action == "save_provider" for action, _ in backend.calls)
    click(root, root.findChild(QQuickItem, "providerSave"))
    action, payload = backend.calls[-1]
    assert action == "save_provider"
    assert payload["provider"]["profile"] == {**original["profile"], "timeout_seconds": timeout}
    assert payload["expected"] == original


def test_dismissed_timeout_edit_is_not_applied_and_new_provider_starts_blank(main):
    root, backend = main
    dialog, original, field = open_provider(root, backend)
    field.setProperty("text", "9")
    QTest.keyClick(root, Qt.Key_Escape)
    wait_for(lambda: not value(dialog, "visible"))
    assert not any(action == "save_provider" for action, _ in backend.calls)
    click(root, visual_item(root, "providerEdit-local"))
    wait_for(lambda: value(dialog, "opened"))
    assert value(field, "text") == "180"
    invoke(dialog, "close")
    wait_for(lambda: not value(dialog, "visible"))
    invoke(dialog, "loadProvider", None)
    wait_for(lambda: value(dialog, "opened"))
    assert value(field, "text") == ""
    root.findChild(QObject, "providerIdField").setProperty("text", "new-local")
    root.findChild(QObject, "providerNameField").setProperty("text", "New local")
    click(root, root.findChild(QQuickItem, "providerSave"))
    action, payload = backend.calls[-1]
    assert action == "save_provider"
    assert payload["provider"]["profile"]["timeout_seconds"] is None
    assert payload["expected"] is None
