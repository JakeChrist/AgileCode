import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: tool
    property var callData: ({})
    property var resultBody: null
    property bool streaming: false
    property bool dark: true
    property bool expanded: false
    readonly property var functionData: callData.function || ({})
    readonly property var argumentsObject: parse(functionData.arguments)
    readonly property var outcome: resultBody ? parse(resultBody.content) : ({})
    readonly property string status: streaming ? "Receiving…" : (!resultBody ? "Requested" : (outcome.status === "error" ? "Failed" : "Returned"))
    readonly property string summary: String(argumentsObject.path || argumentsObject.command || argumentsObject.query || argumentsObject.pattern || argumentsObject.name || "").replace(/\s+/g, " ")
    spacing: 6

    function parse(value) {
        if (typeof value === "object" && value !== null) return value;
        try {
            var parsed = JSON.parse(value || "{}");
            return parsed !== null && typeof parsed === "object" ? parsed : ({});
        } catch (error) { return ({}); }
    }
    function pretty(value) {
        if (typeof value !== "string") return JSON.stringify(value || {}, null, 2);
        try { return JSON.stringify(JSON.parse(value), null, 2); } catch (error) { return value; }
    }

    ToolButton {
        objectName: "toolToggle"
        Layout.fillWidth: true
        checkable: true
        checked: tool.expanded
        onClicked: tool.expanded = checked
        Accessible.name: String(tool.functionData.name || "Tool") + ": " + tool.status
        contentItem: RowLayout {
            spacing: 8
            Text { text: tool.expanded ? "▾" : "▸"; color: tool.dark ? "#a6b8c8" : "#526779" }
            Text {
                text: tool.functionData.name || "Tool"
                textFormat: Text.PlainText
                color: tool.dark ? "#d5e2eb" : "#223647"
                font.family: "monospace"
                font.pixelSize: 12
            }
            Text {
                text: tool.summary
                textFormat: Text.PlainText
                Layout.fillWidth: true
                elide: Text.ElideRight
                color: tool.dark ? "#a6b8c8" : "#526779"
                font.pixelSize: 12
            }
            Text {
                text: (tool.outcome.status === "ok" ? "✓ " : "") + tool.status
                color: tool.outcome.status === "error" ? (tool.dark ? "#efb1a5" : "#a03225") : (tool.dark ? "#a6b8c8" : "#526779")
                font.pixelSize: 11
            }
        }
    }
    TextEdit {
        objectName: "toolDetails"
        visible: tool.expanded
        Layout.fillWidth: true
        Layout.leftMargin: 12
        text: "Arguments\n" + tool.pretty(tool.functionData.arguments) + (tool.resultBody ? "\n\nResult\n" + tool.pretty(tool.resultBody.content) : "")
        textFormat: TextEdit.PlainText
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.Wrap
        color: tool.dark ? "#b6c7d5" : "#435969"
        selectionColor: "#406959"
        selectedTextColor: "#ffffff"
        font.family: "monospace"
        font.pixelSize: 12
    }
}
