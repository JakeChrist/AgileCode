import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// All tool inputs and results share this single disclosure, for both live
// calls and history. The display does not modify the stored tool protocol.
ColumnLayout {
    id: tool
    property var callData: ({})
    property var resultBody: null
    property bool streaming: false
    property bool dark: true
    property bool expandedByDefault: false
    property bool expanded: false
    onExpandedByDefaultChanged: expanded = expandedByDefault
    Component.onCompleted: expanded = expandedByDefault
    signal toggling()
    signal userToggled(bool expanded)

    readonly property var functionData: object(callData && callData.function)
    readonly property string toolName: String(functionData.name || "Tool result")
    readonly property var argumentsValue: parse(functionData.arguments)
    readonly property var argumentsObject: object(argumentsValue)
    readonly property bool hasResult: resultBody !== null && resultBody !== undefined
    readonly property var outcomeValue: hasResult ? parse(resultBody.content) : undefined
    readonly property var outcome: object(outcomeValue)
    readonly property var resultData: object(outcome.data)
    readonly property bool commandTool: toolName === "run_command" || toolName === "git_command"
    readonly property string commandText: typeof argumentsObject.command === "string" ? argumentsObject.command : ""
    readonly property bool hasExitCode: resultData.exit_code !== undefined && resultData.exit_code !== null
    readonly property string commandError: errorSummary()
    readonly property bool cancelled: !!outcome.cancelled_by_user && outcome.status === "error"
    readonly property bool failed: hasResult && !cancelled && (outcome.status === "error" || outcome.isError === true || resultData.isError === true || (commandTool && hasExitCode && Number(resultData.exit_code) !== 0))
    readonly property string status: !hasResult ? (streaming ? "Receiving…" : "Requested") : (cancelled ? "Cancelled by user" : (failed ? "Failed" : "Returned"))
    readonly property string summary: argumentSummary()
    readonly property bool invalidArguments: typeof functionData.arguments === "string" && !validJson(functionData.arguments)
    readonly property bool incompleteArguments: typeof functionData.arguments === "string" && !validJson(functionData.arguments) && streaming
    spacing: 6

    function object(value) {
        return value !== null && typeof value === "object" && !Array.isArray(value) ? value : ({});
    }
    function parse(value) {
        // Preserve primitive results, empty strings and unfinished input text.
        // Invalid/non-JSON content is displayed literally rather than dropped.
        if (typeof value !== "string") return value;
        try { return JSON.parse(value); } catch (error) { return value; }
    }
    function validJson(value) {
        try { JSON.parse(value); return true; } catch (error) { return false; }
    }
    function argumentSummary() {
        var keys = ["path", "destination", "query", "pattern", "name", "title", "job_id"];
        for (var i = 0; i < keys.length; ++i) {
            var value = argumentsObject[keys[i]];
            if (value !== undefined && value !== null && typeof value !== "object" && value !== "") return String(value).replace(/\s+/g, " ");
        }
        return "";
    }
    function errorSummary() {
        if (typeof outcome.error === "string") return outcome.error;
        var error = object(outcome.error);
        if (error.message !== undefined && error.message !== null) return String(error.message);
        if (error.code !== undefined && error.code !== null) return String(error.code);
        return "";
    }
    function resultSummary() {
        if (!hasResult) return "";
        if (cancelled) return commandError || "Cancelled by user. Partial effects may remain.";
        if (failed) return commandError || (commandTool && hasExitCode ? "Exit " + resultData.exit_code : "Tool returned an error.");
        var data = resultData;
        if (commandTool && hasExitCode) return "Exit " + data.exit_code + (data.partial ? " · partial output" : "");
        if (Array.isArray(data.files)) return data.files.length + " files" + (data.partial ? " · partial listing" : "");
        if (Array.isArray(data.matches)) return data.matches.length + " matches" + (data.partial ? " · partial results" : "");
        if (data.total_lines !== undefined) return data.total_lines + " total lines" + (data.partial ? " · partial read" : "");
        if (data.bytes !== undefined) return data.bytes + " bytes" + (data.path ? " · " + data.path : "");
        if (typeof data.summary === "string") return data.summary;
        if (typeof data.status === "string") return data.status;
        if (data.path) return String(data.path);
        if (data.stopped) return "Stopped";
        if (data.id && data.title) return String(data.title);
        return "Completed";
    }

    ToolButton {
        id: toolToggle
        objectName: "toolToggle"
        Layout.fillWidth: true
        checkable: true
        checked: tool.expanded
        onClicked: { tool.toggling(); tool.expanded = checked; tool.userToggled(checked); }
        Accessible.name: tool.toolName + ": " + tool.status
        contentItem: RowLayout {
            spacing: 8
            Text {
                text: tool.expanded ? "▾" : "▸"
                color: tool.dark ? "#a6b8c8" : "#526779"
            }
            Text {
                text: tool.toolName
                textFormat: Text.PlainText
                color: tool.dark ? "#d5e2eb" : "#223647"
                font.family: "monospace"
                font.pixelSize: 12
            }
            Text {
                text: tool.summary
                visible: tool.summary.length > 0
                textFormat: Text.PlainText
                Layout.fillWidth: true
                elide: Text.ElideRight
                color: tool.dark ? "#a6b8c8" : "#526779"
                font.pixelSize: 12
            }
            Item { Layout.fillWidth: tool.summary.length === 0 }
            Text {
                text: (tool.status === "Returned" ? "✓ " : tool.failed ? "✕ " : "") + tool.status
                color: tool.failed ? (tool.dark ? "#efb1a5" : "#a03225") :
                       (tool.status === "Returned" ? (tool.dark ? "#8ad8bd" : "#39745c") :
                        (tool.dark ? "#a6b8c8" : "#526779"))
                font.pixelSize: 11
            }
        }
    }
    // A command is never hidden inside an arguments envelope.
    TextEdit {
        objectName: "shellCommandText"
        visible: tool.commandTool && tool.commandText.length > 0
        Layout.fillWidth: true
        Layout.leftMargin: 12
        text: tool.commandTool ? tool.commandText : ""
        textFormat: TextEdit.PlainText
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.WrapAnywhere
        color: tool.dark ? "#d5e2eb" : "#223647"
        font.family: "monospace"
        font.pixelSize: 12
    }
    Text {
        objectName: "toolResultSummary"
        visible: tool.hasResult && !tool.expanded
        Layout.fillWidth: true
        Layout.leftMargin: 12
        text: tool.resultSummary()
        textFormat: Text.PlainText
        wrapMode: Text.WrapAnywhere
        color: tool.failed ? (tool.dark ? "#efb1a5" : "#a03225") : (tool.dark ? "#a6b8c8" : "#526779")
        font.pixelSize: 11
    }

    ColumnLayout {
        objectName: "toolExpandedContent"
        visible: tool.expanded
        Layout.fillWidth: true
        Layout.leftMargin: 12
        spacing: 10
        TextEdit {
            Layout.fillWidth: true
            text: tool.incompleteArguments ? "Receiving inputs…" : tool.invalidArguments ? "Inputs (invalid or incomplete; original text below)" : "Inputs"
            visible: tool.toolName !== "Tool result" || tool.functionData.arguments !== ""
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }
        ToolData {
            id: inputFields
            objectName: "toolInputs"
            Layout.fillWidth: true
            visible: !tool.incompleteArguments && (tool.toolName !== "Tool result" || tool.functionData.arguments !== "")
            payload: tool.expanded ? tool.argumentsValue : undefined
            area: "inputs"
            toolName: tool.toolName
            commandShown: tool.commandTool && tool.commandText.length > 0
            dark: tool.dark
        }
        TextEdit {
            Layout.fillWidth: true
            visible: tool.hasResult
            text: "Result"
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }
        ToolData {
            objectName: "toolResult"
            Layout.fillWidth: true
            visible: tool.hasResult
            payload: tool.expanded && tool.hasResult ? tool.outcomeValue : undefined
            inputPayload: tool.argumentsValue
            area: "result"
            toolName: tool.toolName
            dark: tool.dark
        }
    }
}
