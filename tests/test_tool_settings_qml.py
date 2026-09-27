"""Exercise the tool settings controls through Qt, including persisted values."""
import json
import os
from pathlib import Path

import pytest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
pytest.importorskip("PySide6")
from PySide6.QtCore import QUrl, QMetaObject, Q_ARG, Qt, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlEngine, QQmlComponent, qmlRegisterType
from PySide6.QtQuick import QQuickWindow
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


@pytest.fixture(scope="module")
def qt():
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    qmlRegisterType(MarkdownPresentation, "AgileCode.Desktop", 1, 0, "MarkdownPresentation")
    qmlRegisterType(ChatDocumentStyle, "AgileCode.Desktop", 1, 0, "ChatDocumentStyle")
    return app


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
    root.setProperty("settings", settings)
    invoke(root, "loadSettings")
    editor = root.findChild(QObject, "commandRules")
    assert value(editor, "rules") == settings["command_rules"]
    editor.setProperty("rules", [{"pattern": "sudo *", "action": "deny"}])
    invoke(root, "saveBasicSettings")
    action, payload = backend.calls[-1]
    assert action == "settings" and payload["scope"] == "project"
    assert payload["settings"]["command_rules"] == [{"pattern": "sudo *", "action": "deny"}]
    assert Settings.model_validate(payload["settings"]).autonomy == "full"
    root.setProperty("closingConfirmed", True)
    root.close()
    root.deleteLater()
    QTest.qWait(1)
