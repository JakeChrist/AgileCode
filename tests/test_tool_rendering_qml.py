"""Exercise complete tool presentations in real Qt Quick controls/documents."""
import json

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QUrl, Qt, QPointF
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from test_conversation_qml import (
    QML, qt, value, invoke, wait_for, visual_items, text_document, tool_field,
)


@pytest.fixture
def tool_view(qt):
    engine = QQmlEngine()
    warnings = []
    engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
    component = QQmlComponent(engine)
    component.setData(b'''
        import QtQuick
        Flickable {
            width: 720; height: 560; clip: true
            contentWidth: width
            contentHeight: tool.implicitHeight + 24
            ToolCall {
                id: tool
                objectName: "renderedTool"
                x: 12; y: 12; width: parent.width - 24
            }
        }
    ''', QUrl.fromLocalFile(str(QML / "ToolRenderingTest.qml")))
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    window = QQuickWindow()
    window.resize(720, 560)
    root.setParentItem(window.contentItem())
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    tool = root.findChild(QQuickItem, "renderedTool")
    QTest.qWait(20)
    yield root, tool, window
    window.close()
    root.setParentItem(None)
    root.deleteLater()
    QTest.qWait(1)
    assert not warnings


def populate(tool, name, arguments, result=None, *, encoded=True):
    tool.setProperty("callData", {"id": "call", "function": {
        "name": name, "arguments": json.dumps(arguments) if encoded else arguments,
    }})
    if result is not None:
        tool.setProperty("resultBody", {"content": json.dumps(result) if encoded else result})


def expand(root, tool, window):
    assert not value(tool, "expanded")
    button = tool.findChild(QQuickItem, "toolToggle")
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                     button.mapToScene(button.boundingRect().center()).toPoint())
    wait_for(lambda: value(tool, "expanded"))
    QTest.qWait(40)
    assert tool.findChild(QQuickItem, "toolDetails") is None
    assert tool.findChild(QQuickItem, "toolRawToggle") is None
    buttons = [item for item in visual_items(tool)
               if value(item, "checkable") is not None and item.isVisible()]
    assert [item.objectName() for item in buttons] == ["toolToggle"]


def field(tool, area, path):
    widget = tool_field(tool, "toolValue-" + area + "/" + path)
    assert widget is not None and widget.isVisible(), path
    assert value(widget, "readOnly") and value(widget, "selectByMouse")
    return widget


def assert_literal(tool, area, path, expected):
    widget = field(tool, area, path)
    assert value(widget, "text") == expected
    assert text_document(widget).toPlainText() == expected
    return widget


@pytest.mark.parametrize("dark", [True, False])
def test_one_expansion_displays_all_nested_inputs_results_and_errors(tool_view, dark):
    root, tool, window = tool_view
    tool.setProperty("dark", dark)
    long_text = "A long field which used to be silently omitted. " * 40 + "\nLAST LINE"
    args = {"path": "src/main.py", "recursive": False, "offset": 0, "cursor": None,
            "empty_text": "", "empty_list": [], "empty_object": {},
            "extension": {"unknown": [{"message": long_text}, False, 0, None, "", [], {}]},
            "literal": '<img src="file:///tmp/private.png"> **literal** [link](javascript:alert(1))'}
    result = {"status": "error", "_execution_id": "exec-123", "cancelled_by_user": False,
              "error": {"code": "ENOENT", "message": "Missing file", "details": {"attempts": [0, False]}},
              "data": {"hash": "abc", "before_hash": "def", "artifact_id": "art-1", "job_id": "job-1",
                       "complete": False, "encoding": "utf-8", "newline": "\r\n", "result": {"unknown": long_text}}}
    populate(tool, "read_file", args, result)
    assert value(tool, "status") == "Failed"
    expand(root, tool, window)
    for path, expected in [("path", "src/main.py"), ("recursive", "false"), ("offset", "0"),
                           ("cursor", "null"), ("empty_text", "Empty text"),
                           ("empty_list", "Empty list"), ("empty_object", "Empty object"),
                           ("extension/unknown/0/message", long_text), ("extension/unknown/1", "false"),
                           ("extension/unknown/2", "0"), ("extension/unknown/3", "null"),
                           ("extension/unknown/4", "Empty text"), ("extension/unknown/5", "Empty list"),
                           ("extension/unknown/6", "Empty object"), ("literal", args["literal"])]:
        assert_literal(tool, "inputs", path, expected)
    for path, expected in [("status", "error"), ("_execution_id", "exec-123"), ("cancelled_by_user", "false"),
                           ("error/code", "ENOENT"), ("error/message", "Missing file"),
                           ("error/details/attempts/0", "0"), ("error/details/attempts/1", "false"),
                           ("data/hash", "abc"), ("data/before_hash", "def"), ("data/artifact_id", "art-1"),
                           ("data/job_id", "job-1"), ("data/complete", "false"), ("data/encoding", "utf-8"),
                           ("data/result/unknown", long_text)]:
        assert_literal(tool, "result", path, expected)
    # Qt normalizes CRLF for display, while retaining the exact source string.
    assert value(field(tool, "result", "data/newline"), "sourceText") == "\r\n"
    assert any(value(item, "text") == "Whitespace only: CR LF (2 characters)" for item in visual_items(tool))
    last = field(tool, "result", "data/result/unknown")
    root.setProperty("contentY", max(0, last.mapToItem(value(root, "contentItem"), QPointF()).y() - 20))
    QTest.qWait(30)
    assert 0 <= last.mapToScene(QPointF()).y() < window.height()
    assert not window.grabWindow().isNull()
    # Verify the actual selectable Qt text control copies the complete value.
    last.forceActiveFocus()
    invoke(last, "selectAll")
    QTest.keyClick(window, Qt.Key_C, Qt.ControlModifier)
    assert QGuiApplication.clipboard().text() == long_text


@pytest.mark.parametrize("payload,expected", [
    (None, "null"), (False, "false"), (0, "0"), ("", "Empty text"),
    ([], "Empty list"), ({}, "Empty object"), ("plain\ntext", "plain\ntext"),
])
def test_top_level_primitive_inputs_and_results_are_not_replaced_with_empty_objects(tool_view, payload, expected):
    root, tool, window = tool_view
    populate(tool, "extension_tool", payload)
    tool.setProperty("resultBody", {"content": json.dumps(payload)})
    expand(root, tool, window)
    assert_literal(tool, "inputs", "", expected)
    assert_literal(tool, "result", "", expected)


def test_file_lists_matches_replacements_and_mcp_preserve_every_entry_field(tool_view):
    root, tool, window = tool_view
    populate(tool, "mcp_extension", {
        "replacements": [{"old": "a\nb", "new": "c\nd", "expected_count": 0, "extension": {"flag": False}}],
    }, {"status": "ok", "data": {
        "files": [{"path": "source.py", "directory": False, "bytes": 0, "custom": "retained"}],
        "matches": [{"path": "source.py", "start_line": 0, "context": " line one\n line two ", "extra": {"column": 0}}],
        "content": [{"type": "text", "text": "**literal MCP**"}, {"type": "resource", "resource": {"uri": "file:///a", "text": "payload"}}],
        "structuredContent": {"nested": [False, None, {"future_field": "complete"}]}, "isError": True,
    }})
    assert value(tool, "status") == "Failed"
    expand(root, tool, window)
    for area, path, expected in [
        ("inputs", "replacements/0/old", "a\nb"), ("inputs", "replacements/0/new", "c\nd"),
        ("inputs", "replacements/0/expected_count", "0"), ("inputs", "replacements/0/extension/flag", "false"),
        ("result", "data/files/0/custom", "retained"), ("result", "data/files/0/bytes", "0"),
        ("result", "data/matches/0/context", " line one\n line two "), ("result", "data/matches/0/extra/column", "0"),
        ("result", "data/content/0/text", "**literal MCP**"), ("result", "data/content/1/resource/text", "payload"),
        ("result", "data/structuredContent/nested/0", "false"), ("result", "data/structuredContent/nested/1", "null"),
        ("result", "data/structuredContent/nested/2/future_field", "complete"), ("result", "data/isError", "true"),
    ]:
        assert_literal(tool, area, path, expected)


def test_safe_prose_retains_html_image_urls_links_and_code_as_visible_text(tool_view):
    root, tool, window = tool_view
    prose = ('## Result\n\n**Readable** output.\n\n'
             '<img src="file:///tmp/private.png">\n\n'
             '![Proof](https://example.test/image.png "Evidence")\n\n'
             '[Unsafe link](javascript:alert(1) "Link title")\n\n'
             '[![Nested image](https://example.test/nested.png)](https://example.test/source "Outer title")\n\n'
             '```html\n<div>&literal</div>\n```')
    populate(tool, "create_ticket", {"title": "Proof", "description": prose, "epic_id": None},
             {"status": "ok", "data": {"id": "T-1", "description": "Returned description", "extra": False}})
    expand(root, tool, window)
    output = tool_field(tool, "toolProseOutput")
    assert output is not None and output.isVisible()
    doc = text_document(output)
    plain = doc.toPlainText()
    assert "Readable output." in plain and "**Readable**" not in plain
    for text in ['<img src="file:///tmp/private.png">', "Image: Proof", "https://example.test/image.png",
                 "Evidence", "Unsafe link (javascript:alert(1))", "Link title", "https://example.test/source", "Outer title", "<div>&literal</div>"]:
        assert text in plain
    block = doc.begin()
    while block.isValid():
        fragments = block.begin()
        while not fragments.atEnd():
            fmt = fragments.fragment().charFormat()
            assert not fmt.isImageFormat() and not fmt.isAnchor()
            fragments += 1
        block = block.next()
    assert_literal(tool, "inputs", "epic_id", "null")
    assert_literal(tool, "result", "data/extra", "false")
    assert "Returned description" in text_document(field(tool, "result", "data/description")).toPlainText()


def test_finish_step_echoed_prose_is_visible_once_and_changed_results_are_complete(tool_view):
    root, tool, window = tool_view
    populate(tool, "finish_step", {"status": "PASS", "summary": "**All checked**", "output": "## Deliverable\n\nLong output"},
             {"status": "ok", "data": {"summary": "**All checked**", "output": "## Deliverable\n\nLong output", "checks": [{"passed": False}]}})
    expand(root, tool, window)
    assert text_document(tool_field(tool, "toolProseSummary")).toPlainText() == "All checked"
    assert "Long output" in text_document(tool_field(tool, "toolProseOutput")).toPlainText()
    assert_literal(tool, "result", "data/summary", "Same as input Summary (shown above)")
    assert_literal(tool, "result", "data/output", "Same as input Output (shown above)")
    assert_literal(tool, "result", "data/checks/0/passed", "false")
    tool.setProperty("resultBody", {"content": json.dumps({"status": "error", "data": {"output": "**Changed** output"}})})
    QTest.qWait(30)
    assert "Changed output" == text_document(field(tool, "result", "data/output")).toPlainText()


def test_partial_streaming_inputs_and_unstructured_errors_remain_visible(tool_view):
    root, tool, window = tool_view
    tool.setProperty("streaming", True)
    populate(tool, "read_file", '{"path":"unfinished', encoded=False)
    expand(root, tool, window)
    assert value(tool, "incompleteArguments")
    assert not tool.findChild(QQuickItem, "toolInputs").isVisible()
    assert any(value(item, "text") == "Receiving inputs…" for item in visual_items(tool))
    # Once a malformed stream stops, explain the issue and preserve its text.
    tool.setProperty("streaming", False)
    assert_literal(tool, "inputs", "", '{"path":"unfinished')
    assert any(value(item, "text") == "Inputs (invalid or incomplete; original text below)" for item in visual_items(tool))
    tool.setProperty("callData", {"function": {"name": "read_file", "arguments": '{"path":"finished.py","offset":0}'}})
    QTest.qWait(30)
    assert not value(tool, "incompleteArguments")
    assert_literal(tool, "inputs", "path", "finished.py")
    assert_literal(tool, "inputs", "offset", "0")
    tool.setProperty("resultBody", {"content": "Failure\n<html>literal</html>"})
    QTest.qWait(30)
    assert_literal(tool, "result", "", "Failure\n<html>literal</html>")


def test_deep_unknown_fields_remain_readable_and_literal_keys_are_preserved(tool_view):
    root, tool, window = tool_view
    nested = {"": "empty key", "a/b~c": "escaped key", "leaf": "deep value"}
    for _ in range(16):
        nested = {"level": nested}
    populate(tool, "unknown_extension", nested)
    expand(root, tool, window)
    path = "/".join(["level"] * 16)
    deepest = assert_literal(tool, "inputs", path + "/leaf", "deep value")
    assert deepest.width() > 300
    assert_literal(tool, "inputs", path + "/", "empty key")
    assert_literal(tool, "inputs", path + "/a~1b~0c", "escaped key")


@pytest.mark.parametrize("kind", ["literal", "command", "prose"])
def test_large_unbroken_values_wrap_without_truncation_or_slow_word_search(tool_view, kind):
    from time import monotonic
    root, tool, window = tool_view
    content = "x" * 80000
    start = monotonic()
    if kind == "command":
        populate(tool, "run_command", {"command": content}, {"status": "ok", "data": {"output": content, "exit_code": 0}})
    elif kind == "prose":
        populate(tool, "finish_step", {"output": content})
    else:
        populate(tool, "unknown_extension", {}, {"status": "ok", "data": {"long": content}})
    expand(root, tool, window)
    widget = (tool_field(tool, "shellCommandOutput") if kind == "command" else
              tool_field(tool, "toolProseOutput") if kind == "prose" else
              field(tool, "result", "data/long"))
    assert widget is not None and widget.isVisible()
    assert text_document(widget).toPlainText() == content
    assert value(widget, "selectByMouse") and value(widget, "readOnly")
    assert widget.height() > 1000  # Wrapped full text, not a clipped one-line summary.
    assert monotonic() - start < 8  # Qt word-boundary fallback used to take ~35 seconds.
    if kind == "command":
        assert value(tool.findChild(QQuickItem, "shellCommandText"), "text") == content
