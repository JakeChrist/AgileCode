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
    signal toggling()
    readonly property var functionData: callData.function || ({})
    readonly property var argumentsObject: parse(functionData.arguments)
    readonly property var outcome: resultBody ? parse(resultBody.content) : ({})
    readonly property bool commandTool: functionData.name === "run_command" || functionData.name === "git_command"
    readonly property string commandText: typeof argumentsObject.command === "string" ? argumentsObject.command : ""
    readonly property var resultData: outcome.data && typeof outcome.data === "object" ? outcome.data : ({})
    readonly property bool hasExitCode: resultData.exit_code !== undefined && resultData.exit_code !== null
    readonly property string commandOutput: typeof resultData.output === "string" ? resultData.output : ""
    readonly property string commandError: outcome.error && outcome.error.message ? String(outcome.error.message) : (typeof outcome.error === "string" ? outcome.error : "")
    readonly property string status: streaming ? "Receiving…" : (!resultBody ? "Requested" : (outcome.status === "error" || (commandTool && hasExitCode && resultData.exit_code !== 0) ? "Failed" : "Returned"))
    readonly property string summary: String(argumentsObject.path || argumentsObject.command || argumentsObject.query || argumentsObject.pattern || argumentsObject.name || "").replace(/\s+/g, " ")
    readonly property bool proseControl: ["finish_step", "ask_user", "update_progress", "record_check"].indexOf(String(functionData.name || "")) >= 0
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
        onClicked: { tool.toggling(); tool.expanded = checked; }
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
                visible: !tool.commandTool
                textFormat: Text.PlainText
                Layout.fillWidth: true
                elide: Text.ElideRight
                color: tool.dark ? "#a6b8c8" : "#526779"
                font.pixelSize: 12
            }
            Text {
                text: (tool.status === "Returned" && tool.outcome.status === "ok" ? "✓ " : "") + tool.status + (tool.commandTool && tool.hasExitCode ? " · exit " + tool.resultData.exit_code : "")
                color: tool.outcome.status === "error" ? (tool.dark ? "#efb1a5" : "#a03225") : (tool.dark ? "#a6b8c8" : "#526779")
                font.pixelSize: 11
            }
        }
    }
    // Shell commands are always visible, even when their output is collapsed.
    // Preserve the exact command and stdout instead of showing escaped JSON.
    ColumnLayout {
        visible: tool.commandTool && tool.commandText.length > 0
        Layout.fillWidth: true
        Layout.leftMargin: 12
        spacing: 5

        Text {
            text: "Command"
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        TextEdit {
            objectName: "shellCommandText"
            Layout.fillWidth: true
            text: tool.commandText
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
            color: tool.dark ? "#d5e2eb" : "#223647"
            font.family: "monospace"
            font.pixelSize: 12
        }
        Text {
            visible: tool.expanded && !!tool.resultData.cwd
            text: "Working directory: " + (tool.resultData.cwd || "")
            textFormat: Text.PlainText
            wrapMode: Text.WrapAnywhere
            Layout.fillWidth: true
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
        }
        Text {
            visible: tool.expanded && !!tool.resultBody
            text: "Output"
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        TextEdit {
            objectName: "shellCommandOutput"
            visible: tool.expanded && !!tool.resultBody
            Layout.fillWidth: true
            text: tool.commandOutput || tool.commandError || "(no output)"
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
            color: tool.dark ? "#d5e2eb" : "#223647"
            font.family: "monospace"
            font.pixelSize: 12
        }
        Text {
            visible: tool.expanded && tool.resultData.partial === true
            text: "Partial output · additional output may be available in the saved artifact."
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
        }
    }

    ColumnLayout {
        visible: tool.expanded && tool.proseControl
        Layout.fillWidth: true
        Layout.leftMargin: 12
        spacing: 7

        Text {
            visible: tool.functionData.name === "finish_step"
            text: String(tool.argumentsObject.status || "")
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        RenderedText {
            objectName: "toolProseSummary"
            visible: tool.functionData.name === "finish_step" && String(tool.argumentsObject.summary || "").length > 0
            Layout.fillWidth: true
            sourceText: String(tool.argumentsObject.summary || "")
            dark: tool.dark
            color: tool.dark ? "#c7d5e0" : "#435969"
            font.pixelSize: 12
        }
        RenderedText {
            objectName: "toolProseOutput"
            visible: tool.functionData.name === "finish_step" && String(tool.argumentsObject.output || "").length > 0
            Layout.fillWidth: true
            sourceText: String(tool.argumentsObject.output || "")
            dark: tool.dark
            color: tool.dark ? "#d5e2eb" : "#334b5c"
            font.pixelSize: 12
        }
        RenderedText {
            visible: tool.functionData.name === "ask_user"
            Layout.fillWidth: true
            sourceText: String(tool.argumentsObject.question || "")
            dark: tool.dark
            font.pixelSize: 12
        }
        RenderedText {
            visible: tool.functionData.name === "update_progress"
            Layout.fillWidth: true
            sourceText: String(tool.argumentsObject.notes || "")
            dark: tool.dark
            font.pixelSize: 12
        }
        RenderedText {
            visible: tool.functionData.name === "record_check"
            Layout.fillWidth: true
            sourceText: String(tool.argumentsObject.method || "")
            dark: tool.dark
            font.pixelSize: 12
        }
    }
    TextEdit {
        objectName: "toolDetails"
        visible: tool.expanded && !tool.proseControl && !tool.commandTool
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
