"""Exercise the tool settings controls through Qt, including persisted values."""
import json
import os
from pathlib import Path
from time import monotonic

import pytest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
pytest.importorskip("PySide6")
from PySide6.QtCore import QUrl, QMetaObject, Q_ARG, Qt, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlEngine, QQmlComponent, qmlRegisterType
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from agilecode.core.defaults import standard_team
from agilecode.core.types import Settings
from agilecode.desktop.chatstyle import MarkdownPresentation, ChatDocumentStyle

QML = Path(__file__).parents[1] / "src/agilecode/desktop/qml"


def value(item, name):
    result = item.property(name)
    return result.toVariant() if hasattr(result, "toVariant") else result


def invoke(item, method, *args):
    assert QMetaObject.invokeMethod(item, method, Qt.DirectConnection, *(Q_ARG("QVariant", x) for x in args))


def wait_for(predicate, timeout_ms=1000):
    deadline = monotonic() + timeout_ms / 1000
    while not predicate() and monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "Qt condition did not become true before the deadline"


def settings_snapshots(settings, revision="original"):
    return {scope: {"scope": scope, "values": settings, "effective": settings,
                    "revision": revision + "-" + scope} for scope in ("project", "global")}


def send_snapshot(backend, settings, revision="original"):
    # Use the real JSON signal boundary; QVariant Python None becomes undefined
    # in JavaScript and would incorrectly remove nullable fields from the base.
    backend.response.emit("refresh", "snapshot", json.dumps({"ok": True, "data": {
        "board": [], "teams": [], "skills": [], "settings": settings,
        "settings_snapshots": settings_snapshots(settings, revision),
    }}))


def load_settings(root, backend, settings):
    send_snapshot(backend, settings)


def click(window, item):
    assert item is not None
    wait_for(lambda: item.isVisible() and item.width() > 0 and item.height() > 0)
    QTest.qWait(1)  # Apply queued layout polish before mapping pointer coordinates.
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     item.mapToScene(item.boundingRect().center()).toPoint())


def visual_item(root, name):
    refs = []
    def visit(item):
        refs.append(item)
        if item.objectName() == name:
            return item
        for child in item.childItems():
            result = visit(child)
            if result is not None:
                return result
        return None
    result = visit(root.contentItem())
    root._visual_refs = refs
    return result


@pytest.fixture(scope="module")
def qt():
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setOrganizationName("AgileCodeTests")
    app.setApplicationName("AgileCodeTests")
    qmlRegisterType(MarkdownPresentation, "AgileCode.Desktop", 1, 0, "MarkdownPresentation")
    qmlRegisterType(ChatDocumentStyle, "AgileCode.Desktop", 1, 0, "ChatDocumentStyle")
    return app


@pytest.fixture
def main(qt):
    class Backend(QObject):
        response = Signal(str, str, str)
        event = Signal(str)
        ready = Signal(str)
        fatal = Signal(str)

        def __init__(self):
            super().__init__()
            self.calls = []

        @Slot(str, str, result=str)
        def dispatch(self, action, payload):
            self.calls.append((action, json.loads(payload)))
            return str(len(self.calls))

    engine = QQmlEngine()
    backend = Backend()
    engine.rootContext().setContextProperty("backend", backend)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "Main.qml")))
    root = component.create()
    assert root is not None, [e.toString() for e in component.errors()]
    assert QTest.qWaitForWindowExposed(root)
    yield root, backend
    root.setProperty("closingConfirmed", True)
    root.close()
    root.deleteLater()
    QTest.qWait(1)


def test_command_rules_reorder_and_round_trip(qt):
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "CommandRules.qml")))
    rules = [{"pattern": "pip install *", "action": "ask"}, {"pattern": "pytest *", "action": "allow"}]
    item = component.createWithInitialProperties({"rules": rules, "width": 800})
    assert item is not None, [e.toString() for e in component.errors()]
    invoke(item, "moveRule", 1, -1)
    assert value(item, "rules")[0] == rules[1]
    invoke(item, "updateRule", 0, "action", "deny")
    assert value(item, "rules")[0]["action"] == "deny"
    invoke(item, "removeRule", 1)
    assert len(Settings(command_rules=value(item, "rules")).command_rules) == 1
    item.deleteLater()
    QTest.qWait(1)


def test_member_selection_saves_exact_names_and_preserves_offline_mcp(qt):
    engine = QQmlEngine()
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "TeamManager.qml")))
    team = standard_team().model_dump(mode="json")
    team["members"][0]["allowed_tools"] = ["read_file", "mcp_offline_123"]
    item = component.createWithInitialProperties({"width": 1000, "height": 800})
    assert item is not None, [e.toString() for e in component.errors()]
    invoke(item, "selectTeam", {"ref": "project:standard", "scope": "project", "team": team})
    invoke(item, "toggleTool", "run_command", True)
    assert value(item, "draft")["members"][0]["allowed_tools"] == ["read_file", "mcp_offline_123", "run_command"]
    invoke(item, "toggleTool", "read_file", False)
    assert value(item, "draft")["members"][0]["allowed_tools"] == ["mcp_offline_123", "run_command"]
    item.deleteLater()
    QTest.qWait(1)


def test_main_settings_load_and_save_command_rules(qt):
    class Backend(QObject):
        response = Signal(str, str, str)
        event = Signal(str)
        ready = Signal(str)
        fatal = Signal(str)
        def __init__(self):
            super().__init__()
            self.calls = []
        @Slot(str, str, result=str)
        def dispatch(self, action, payload):
            self.calls.append((action, json.loads(payload)))
            return str(len(self.calls))
    engine = QQmlEngine()
    backend = Backend()
    engine.rootContext().setContextProperty("backend", backend)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "Main.qml")))
    root = component.create()
    assert root is not None, [e.toString() for e in component.errors()]
    settings = Settings(autonomy="full", command_rules=[{"pattern": "pip install *", "action": "ask"}]).model_dump(mode="json")
    load_settings(root, backend, settings)
    editor = root.findChild(QObject, "commandRules")
    assert value(editor, "rules") == settings["command_rules"]
    editor.setProperty("rules", [{"pattern": "sudo *", "action": "deny"}])
    root.findChild(QObject, "pythonEnvironmentField").setProperty("currentIndex", 1)
    root.findChild(QObject, "pythonInterpreterField").setProperty("text", "/usr/bin/python3.13")
    root.findChild(QObject, "setupCommandsField").setProperty("text", "python -m pip install -e '.[test]'\nexport TESTING=1")
    # Tool settings live on their own page and save independently.
    invoke(root, "saveToolSettings")
    tool_action, tool_payload = backend.calls[-1]
    assert tool_action == "settings" and tool_payload["scope"] == "project"
    assert tool_payload["settings"]["command_rules"] == [{"pattern": "sudo *", "action": "deny"}]
    assert Settings.model_validate(tool_payload["settings"]).command_rules[0].pattern == "sudo *"

    # Saving application settings cannot accidentally overwrite edited tool rules.
    invoke(root, "saveBasicSettings")
    action, payload = backend.calls[-1]
    assert action == "settings" and payload["scope"] == "project"
    assert payload["settings"]["command_rules"] == settings["command_rules"]
    saved = Settings.model_validate(payload["settings"])
    assert saved.autonomy == "full"
    assert saved.python_environment == "venv"
    assert saved.python_interpreter == "/usr/bin/python3.13"
    assert saved.setup_commands == ["python -m pip install -e '.[test]'", "export TESTING=1"]
    root.setProperty("closingConfirmed", True)
    root.close()
    root.deleteLater()
    QTest.qWait(1)


def test_main_operational_limits_and_mcp_are_separate_from_inference(qt):
    class Backend(QObject):
        response = Signal(str, str, str)
        event = Signal(str)
        ready = Signal(str)
        fatal = Signal(str)

        def __init__(self):
            super().__init__()
            self.calls = []

        @Slot(str, str, result=str)
        def dispatch(self, action, payload):
            self.calls.append((action, json.loads(payload)))
            return str(len(self.calls))

    engine = QQmlEngine()
    backend = Backend()
    engine.rootContext().setContextProperty("backend", backend)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "Main.qml")))
    root = component.create()
    assert root is not None, [e.toString() for e in component.errors()]
    load_settings(root, backend, Settings().model_dump(mode="json"))

    # Optional operational limits round-trip an empty field as unlimited.
    corrections = root.findChild(QObject, "correctionLimitField")
    shell_timeout = root.findChild(QObject, "shellTimeoutField")
    assert value(corrections, "text") == "10"
    assert value(shell_timeout, "text") == ""
    corrections.setProperty("text", "")
    root.findChild(QObject, "gitTimeoutField").setProperty("text", "315")
    root.findChild(QObject, "transportRetryField").setProperty("text", "5")
    summary_retries = root.findChild(QObject, "contextSummaryRetryField")
    assert summary_retries is not None and value(summary_retries, "text") == "3"
    summary_retries.setProperty("text", "4")
    root.findChild(QObject, "transportBackoffField").setProperty("text", "17")
    invoke(root, "saveBasicSettings")
    action, payload = backend.calls[-1]
    assert action == "settings"
    saved = Settings.model_validate(payload["settings"])
    assert saved.git_operation_timeout_seconds == 315
    assert saved.model_transport_retry_attempts == 5
    assert saved.context_summarization_retry_attempts == 4
    assert saved.model_transport_retry_delay_cap_seconds == 17
    assert saved.max_corrections is None
    assert saved.shell_command_timeout_seconds is None
    assert not {"max_tokens", "context_window", "max_requests_per_attempt"} & Settings.model_fields.keys()

    root.findChild(QObject, "toolRulesText").setProperty("text", '{"git_command": "ask"}')
    root.findChild(QObject, "mcpServersText").setProperty("text", '[{"id":"local-mcp","transport":"stdio","command":"python","timeout_seconds":null,"max_message_bytes":null}]')
    invoke(root, "saveToolSettings")
    action, payload = backend.calls[-1]
    assert action == "settings"
    saved = Settings.model_validate(payload["settings"])
    assert saved.tool_rules["git_command"] == "ask"
    assert saved.mcp_servers[0].timeout_seconds is None
    assert saved.mcp_servers[0].max_message_bytes is None

    root.setProperty("closingConfirmed", True)
    root.close()
    root.deleteLater()
    QTest.qWait(1)


@pytest.mark.parametrize("scope", ["project", "global"])
@pytest.mark.parametrize("save_method,page,scope_control", [
    ("saveBasicSettings", 5, "settingsScope"),
    ("saveToolSettings", 6, "toolsScope"),
])
def test_settings_save_uses_original_editor_snapshot(main, scope, save_method, page, scope_control):
    root, backend = main
    original = Settings().model_dump(mode="json")
    load_settings(root, backend, original)
    root.setProperty("currentPage", page)
    root.findChild(QObject, scope_control).setProperty("currentIndex", int(scope == "global"))
    root.findChild(QObject, "gitTimeoutField").setProperty("text", "315")
    root.findChild(QObject, "toolRulesText").setProperty("text", '{"git_command":"ask"}')

    updated = {**original, "git_operation_timeout_seconds": 88,
               "remove_worktree_after_merge": False}
    snapshot = {"board": [], "teams": [], "skills": [], "settings": updated,
                "settings_snapshots": settings_snapshots(updated, "newer")}
    backend.response.emit("refresh", "snapshot", json.dumps({"ok": True, "data": snapshot}))
    assert value(root, "settings")["git_operation_timeout_seconds"] == 88
    # A background snapshot cannot erase the visible draft or replace its base.
    assert value(root.findChild(QObject, "gitTimeoutField"), "text") == "315"
    invoke(root, save_method)
    action, payload = backend.calls[-1]
    assert action == "settings" and payload["scope"] == scope
    assert payload["expected"] == settings_snapshots(original)[scope]
    assert payload["settings"]["remove_worktree_after_merge"] is True
    if save_method == "saveBasicSettings":
        assert payload["settings"]["git_operation_timeout_seconds"] == 315
    else:
        assert payload["settings"]["tool_rules"] == {"git_command": "ask"}

    # Successful save explicitly reloads fields and bases, even on this page.
    backend.response.emit("save", "settings", json.dumps({"ok": True, "data": {}}))
    backend.response.emit("refresh", "snapshot", json.dumps({"ok": True, "data": snapshot}))
    assert value(root.findChild(QObject, "gitTimeoutField"), "text") == "88"
    assert value(root, "settingsEditorSnapshots") == settings_snapshots(updated, "newer")


def test_reset_and_advanced_settings_keep_the_loaded_base(main):
    root, backend = main
    original = Settings().model_dump(mode="json")
    load_settings(root, backend, original)
    root.setProperty("currentPage", 5)
    dialog = root.findChild(QObject, "advancedSettingsDialog")
    invoke(dialog, "loadDraft")
    wait_for(lambda: value(dialog, "opened"))
    draft = {**original, "git_operation_timeout_seconds": 75}
    root.findChild(QObject, "advancedSettingsText").setProperty("text", json.dumps(draft))
    # Even an explicit subsequent form load must not update the open JSON draft.
    newer = {**original, "git_operation_timeout_seconds": 99}
    send_snapshot(backend, newer, "newer")
    invoke(root, "loadSettings")
    click(root, root.findChild(QQuickItem, "advancedSettingsSave"))
    assert backend.calls[-1] == ("settings", {
        "settings": draft, "scope": "project",
        "expected": settings_snapshots(original)["project"],
    })
    invoke(dialog, "close")
    wait_for(lambda: not value(dialog, "visible"))
    root.setProperty("settingsSnapshots", settings_snapshots(original, "newest"))
    invoke(root, "resetSettings")
    assert backend.calls[-1] == ("reset_settings", {
        "expected": settings_snapshots(newer, "newer")["project"],
    })


def test_settings_cannot_save_without_a_loaded_snapshot(main):
    root, backend = main
    root.setProperty("settings", Settings().model_dump(mode="json"))
    invoke(root, "loadSettings")
    before = len(backend.calls)
    invoke(root, "saveBasicSettings")
    invoke(root, "saveToolSettings")
    invoke(root, "resetSettings")
    assert len(backend.calls) == before
    assert "Settings have not loaded" in value(root, "statusMessage")


def provider(identity, name=None):
    return {"id": identity, "name": name or identity, "profile": {
        "base_url": "http://127.0.0.1:1234/v1", "model": "fixture", "api_key_env": None,
        "timeout_seconds": None, "request_options": {}, "round_trip_reasoning": False,
    }}


@pytest.mark.parametrize("editing", [True, False])
def test_provider_dialog_saves_original_entry_and_default(main, editing):
    root, backend = main
    original = provider("first")
    backend.response.emit("refresh", "providers", json.dumps({"ok": True, "data": {
        "format_version": 1, "default_provider_id": "first",
        "providers": [original, provider("second")],
    }}))
    root.setProperty("currentPage", 4)
    dialog = root.findChild(QObject, "providerDialog")
    if editing:
        wait_for(lambda: visual_item(root, "providerEdit-first") is not None)
        click(root, visual_item(root, "providerEdit-first"))
    else:
        invoke(dialog, "loadProvider", None)
    wait_for(lambda: value(dialog, "opened"))
    root.findChild(QObject, "providerNameField").setProperty("text", "Edited name")
    if not editing:
        root.findChild(QObject, "providerIdField").setProperty("text", "new-provider")
    backend.response.emit("refresh", "providers", json.dumps({"ok": True, "data": {
        "format_version": 1, "default_provider_id": "second",
        "providers": [provider("first", "Concurrent edit"), provider("second")],
    }}))
    click(root, root.findChild(QQuickItem, "providerSave"))
    action, payload = backend.calls[-1]
    assert action == "save_provider"
    assert payload["provider"]["name"] == "Edited name"
    assert payload["expected"] == (original if editing else None)
    assert payload["expected_default"] == "first"
    assert payload["make_default"] is editing


def test_provider_list_actions_carry_the_displayed_snapshot(main):
    root, backend = main
    first, second = provider("first"), provider("second")
    backend.response.emit("refresh", "providers", json.dumps({"ok": True, "data": {
        "format_version": 1, "default_provider_id": "first", "providers": [first, second],
    }}))
    root.setProperty("currentPage", 4)
    wait_for(lambda: visual_item(root, "providerDefault-second") is not None)
    click(root, visual_item(root, "providerDefault-second"))
    assert backend.calls[-1] == ("default_provider", {
        "provider_id": "second", "expected": second, "expected_default": "first",
    })
    click(root, visual_item(root, "providerDelete-first"))
    assert backend.calls[-1] == ("delete_provider", {
        "provider_id": "first", "expected": first, "expected_default": "first",
    })


@pytest.mark.parametrize("scope,rename", [("project", False), ("global", False), ("global", True)])
def test_team_editor_keeps_original_target_scope_snapshot(main, scope, rename):
    root, backend = main
    original = standard_team().model_dump(mode="json")
    global_team = {**original, "name": "Original global team"}
    catalog = [
        {"ref": "project:standard", "scope": "project", "team": original},
        {"ref": "global:standard", "scope": "global", "team": global_team},
    ]
    settings = Settings().model_dump(mode="json")
    backend.response.emit("initial", "snapshot", json.dumps({"ok": True, "data": {
        "board": [], "teams": catalog, "skills": [], "settings": settings,
        "settings_snapshots": settings_snapshots(settings),
    }}))
    root.setProperty("currentPage", 3)
    manager = root.findChild(QQuickItem, "teamManager")
    assert value(manager, "draft")["name"] == original["name"]
    invoke(manager, "updateTeam", "name", "My draft")
    if rename:
        invoke(manager, "updateTeam", "id", "new-team")
    root.findChild(QObject, "teamSaveScope").setProperty("currentIndex", int(scope == "project"))
    # Catalog changes may refresh the list, never the edited team or its base.
    newer = [{**entry, "team": {**entry["team"], "name": "Other editor"}} for entry in catalog]
    root.setProperty("teams", newer)
    click(root, root.findChild(QQuickItem, "teamSave"))
    action, payload = backend.calls[-1]
    assert action == "save_team" and payload["scope"] == scope
    assert payload["team"]["name"] == "My draft"
    expected = None if rename else original if scope == "project" else global_team
    assert payload["expected"] == expected

    # The returned catalog acknowledges this successful save as the next base.
    ref = scope + ":" + payload["team"]["id"]
    backend.response.emit("saved", "save_team", json.dumps({"ok": True, "data": {"ref": ref}}))
    saved_catalog = [entry for entry in catalog if entry["ref"] != ref]
    saved_catalog.append({"ref": ref, "scope": scope, "team": payload["team"]})
    backend.response.emit("saved-refresh", "snapshot", json.dumps({"ok": True, "data": {
        "board": [], "teams": saved_catalog, "skills": [], "settings": settings,
        "settings_snapshots": settings_snapshots(settings),
    }}))
    invoke(manager, "updateTeam", "name", "Another edit")
    click(root, root.findChild(QQuickItem, "teamSave"))
    assert backend.calls[-1][1]["expected"] == payload["team"]


@pytest.mark.parametrize("new_profile", [False, True])
def test_pm_profile_save_keeps_original_editor_snapshot(main, new_profile):
    root, backend = main
    original = {"id": "profile", "name": "Original", "instructions": "Original role", "skills": []}
    backend.response.emit("profiles", "pm_profiles", json.dumps({"ok": True, "data": [original]}))
    root.setProperty("currentPage", 2)
    root.findChild(QObject, "pmTabs").setProperty("currentIndex", 1)
    invoke(root, "selectPMProfile", 1)
    if new_profile:
        invoke(root, "startNewPMProfile", False)
    root.findChild(QObject, "pmProfileName").setProperty("text", "My profile draft")
    backend.response.emit("refresh", "pm_profiles", json.dumps({"ok": True, "data": [
        {**original, "name": "Concurrent edit", "instructions": "Different role"},
    ]}))
    save = root.findChild(QQuickItem, "pmProfileSave")
    click(root, save)
    action, payload = backend.calls[-1]
    assert action == "pm_profile_save"
    assert payload["profile"]["name"] == "My profile draft"
    assert payload["expected"] == (None if new_profile else original)
    assert payload["profile"]["instructions"] == ("" if new_profile else "Original role")


def test_pm_profile_delete_confirms_the_original_profile(main):
    root, backend = main
    original = {"id": "profile", "name": "Original", "instructions": "Original role", "skills": []}
    backend.response.emit("profiles", "pm_profiles", json.dumps({"ok": True, "data": [original]}))
    root.setProperty("currentPage", 2)
    invoke(root, "selectPMProfile", 1)
    dialog = root.findChild(QObject, "pmProfileDeleteDialog")
    invoke(dialog, "loadProfile")
    wait_for(lambda: value(dialog, "opened"))
    backend.response.emit("refresh", "pm_profiles", json.dumps({"ok": True, "data": [
        {**original, "name": "Concurrent edit"},
    ]}))
    invoke(dialog, "accept")
    assert backend.calls[-1] == ("pm_profile_delete", {"id": "profile", "expected": original})


@pytest.mark.parametrize("page,button", [(5, "settingsReload"), (6, "toolsReload")])
def test_reload_settings_requires_confirmation_and_uses_fresh_snapshot(main, page, button):
    root, backend = main
    original = Settings().model_dump(mode="json")
    load_settings(root, backend, original)
    root.setProperty("currentPage", page)
    field = root.findChild(QObject, "gitTimeoutField")
    rules = root.findChild(QObject, "toolRulesText")
    field.setProperty("text", "315")
    rules.setProperty("text", '{"git_command":"ask"}')
    dialog = root.findChild(QObject, "settingsReloadDialog")
    before = len(backend.calls)
    click(root, root.findChild(QQuickItem, button))
    wait_for(lambda: value(dialog, "opened"))
    invoke(dialog, "reject")
    wait_for(lambda: not value(dialog, "visible"))
    assert len(backend.calls) == before
    assert value(field, "text") == "315"
    assert json.loads(value(rules, "text")) == {"git_command": "ask"}

    click(root, root.findChild(QQuickItem, button))
    wait_for(lambda: value(dialog, "opened"))
    invoke(dialog, "accept")
    assert backend.calls[before:] == [("snapshot", {})]
    identity = str(len(backend.calls))
    assert value(field, "text") == "315"  # Keep the draft until that read returns.
    updated = {**original, "git_operation_timeout_seconds": 88,
               "tool_rules": {"run_command": "deny"}}
    send_snapshot(backend, updated, "newer")  # An unrelated background read is insufficient.
    assert value(field, "text") == "315"
    backend.response.emit(identity, "snapshot", json.dumps({"ok": True, "data": {
        "board": [], "teams": [], "skills": [], "settings": updated,
        "settings_snapshots": settings_snapshots(updated, "newer"),
    }}))
    assert value(field, "text") == "88"
    assert json.loads(value(rules, "text")) == {"run_command": "deny"}
    assert value(root, "settingsEditorSnapshots") == settings_snapshots(updated, "newer")
    assert backend.calls[before:] == [("snapshot", {})]
