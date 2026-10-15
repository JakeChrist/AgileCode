import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One presentation for tool calls and their persisted results.
// The protocol stays structured; raw envelopes are optional diagnostics.
ColumnLayout {
    id: tool
    property var callData: ({})
    property var resultBody: null
    property bool streaming: false
    property bool dark: true
    property bool expanded: false
    property bool rawOpen: false
    signal toggling()

    readonly property var functionData: callData.function || ({})
    readonly property string toolName: String(functionData.name || "Tool result")
    readonly property var argumentsObject: parse(functionData.arguments)
    readonly property var outcome: resultBody ? parse(resultBody.content) : ({})
    readonly property var resultData: outcome.data && typeof outcome.data === "object" ? outcome.data : ({})
    readonly property bool commandTool: toolName === "run_command" || toolName === "git_command"
    readonly property string commandText: typeof argumentsObject.command === "string" ? argumentsObject.command : ""
    readonly property bool hasExitCode: resultData.exit_code !== undefined && resultData.exit_code !== null
    readonly property string commandError: outcome.error && outcome.error.message ? String(outcome.error.message) : (typeof outcome.error === "string" ? outcome.error : "")
    readonly property bool cancelled: !!outcome.cancelled_by_user && outcome.status === "error"
    readonly property bool failed: !!resultBody && !cancelled && (outcome.status === "error" || (commandTool && hasExitCode && Number(resultData.exit_code) !== 0))
    readonly property string status: !resultBody ? (streaming ? "Receiving…" : "Requested") : (cancelled ? "Cancelled by user" : (failed ? "Failed" : "Returned"))
    readonly property string summary: String(argumentsObject.path || argumentsObject.destination || argumentsObject.query || argumentsObject.pattern || argumentsObject.name || argumentsObject.title || argumentsObject.job_id || "").replace(/\s+/g, " ")
    readonly property bool proseControl: ["finish_step", "ask_user", "update_progress", "record_check", "create_ticket"].indexOf(toolName) >= 0
    readonly property string outputText: formatOutput()
    readonly property string detailText: formatDetails()
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
    function formatOutput() {
        var data = resultData;
        if (typeof data.output === "string") return data.output;
        if (typeof data.content === "string") return data.content;
        if (typeof data.text === "string") return data.text;
        if (Array.isArray(data.files)) {
            return data.files.map(function(file) {
                return (file.directory ? "📁 " : "   ") + String(file.path || file);
            }).join("\n");
        }
        if (Array.isArray(data.matches)) {
            return data.matches.map(function(hit) {
                var where = String(hit.path || "");
                if (hit.start_line) where += ":" + hit.start_line;
                return where + (hit.symbol ? "  " + hit.symbol : "") +
                    (hit.context ? "\n    " + String(hit.context).trim() : "");
            }).join("\n\n");
        }
        if (Array.isArray(data.errors) && data.errors.length) {
            return data.errors.map(function(err) { return String(err.path || "") + ": " + String(err.error || err.message || err); }).join("\n");
        }
        if (typeof data.description === "string") return data.description;
        return "";
    }
    function resultSummary() {
        if (!resultBody) return "";
        if (cancelled) return commandError || "Cancelled by user. Partial effects may remain.";
        if (failed) return commandError || "Tool returned an error.";
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
    function formatDetails() {
        var data = resultData;
        var ignore = {
            content: true, output: true, text: true, description: true, files: true, matches: true,
            errors: true, hash: true, before_hash: true, artifact_id: true, job_id: true,
            complete: true, encoding: true, newline: true, result: true
        };
        var lines = [];
        Object.keys(data).forEach(function(key) {
            if (ignore[key]) return;
            var value = data[key];
            if (value === null || value === undefined || typeof value === "object") return;
            if (typeof value === "string" && value.length > 240) return;
            lines.push(key.replace(/_/g, " ") + ": " + value);
        });
        return lines.join("\n");
    }

    ToolButton {
        id: toolToggle
        objectName: "toolToggle"
        Layout.fillWidth: true
        checkable: true
        checked: tool.expanded
        onClicked: { tool.toggling(); tool.expanded = checked; }
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
        wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
        color: tool.dark ? "#d5e2eb" : "#223647"
        font.family: "monospace"
        font.pixelSize: 12
    }
    Text {
        objectName: "toolResultSummary"
        visible: !!tool.resultBody && (tool.failed || !tool.expanded)
        Layout.fillWidth: true
        Layout.leftMargin: 12
        text: tool.resultSummary()
        textFormat: Text.PlainText
        wrapMode: Text.WrapAnywhere
        color: tool.failed ? (tool.dark ? "#efb1a5" : "#a03225") : (tool.dark ? "#a6b8c8" : "#526779")
        font.pixelSize: 11
    }

    ColumnLayout {
        visible: tool.expanded
        Layout.fillWidth: true
        Layout.leftMargin: 12
        spacing: 6
        Text {
            visible: tool.commandTool && !!tool.resultData.cwd
            text: "Working directory: " + (tool.resultData.cwd || "")
            textFormat: Text.PlainText
            wrapMode: Text.WrapAnywhere
            Layout.fillWidth: true
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
        }
        Text {
            visible: tool.proseControl && tool.toolName === "finish_step"
            text: String(tool.argumentsObject.status || "")
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        RenderedText {
            objectName: "toolProseSummary"
            visible: tool.proseControl && tool.toolName === "finish_step" && String(tool.argumentsObject.summary || "").length > 0
            Layout.fillWidth: true
            sourceText: tool.expanded ? String(tool.argumentsObject.summary || "") : ""
            dark: tool.dark
            font.pixelSize: 12
        }
        RenderedText {
            objectName: "toolProseOutput"
            visible: tool.proseControl && ["finish_step", "ask_user", "update_progress", "record_check", "create_ticket"].indexOf(tool.toolName) >= 0
            Layout.fillWidth: true
            sourceText: !tool.expanded ? "" : (tool.toolName === "finish_step" ? String(tool.argumentsObject.output || "") :
                        tool.toolName === "ask_user" ? String(tool.argumentsObject.question || "") :
                        tool.toolName === "update_progress" ? String(tool.argumentsObject.notes || "") :
                        tool.toolName === "record_check" ? String(tool.argumentsObject.method || "") :
                        String(tool.argumentsObject.description || ""))
            dark: tool.dark
            font.pixelSize: 12
        }
        Text {
            visible: tool.resultBody !== null && tool.outputText.length > 0 && !tool.proseControl
            text: "Output"
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        TextEdit {
            objectName: "shellCommandOutput"
            visible: tool.resultBody !== null && tool.outputText.length > 0 && !tool.proseControl
            Layout.fillWidth: true
            text: tool.expanded && !tool.proseControl ? tool.outputText : ""
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
            color: tool.dark ? "#d5e2eb" : "#223647"
            font.family: "monospace"
            font.pixelSize: 12
        }
        Text {
            visible: tool.resultBody !== null && tool.detailText.length > 0 && !tool.proseControl
            Layout.fillWidth: true
            text: tool.expanded ? tool.detailText : ""
            textFormat: Text.PlainText
            wrapMode: Text.WrapAnywhere
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
        }
        Text {
            visible: !!tool.resultData.partial
            text: "Partial result · more data may be available."
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: tool.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 11
        }
        ToolButton {
            objectName: "toolRawToggle"
            text: (tool.rawOpen ? "▾ " : "▸ ") + "Raw details"
            font.pixelSize: 11
            palette.buttonText: tool.dark ? "#a6b8c8" : "#526779"
            onClicked: tool.rawOpen = !tool.rawOpen
        }
        TextEdit {
            objectName: "toolDetails"
            visible: tool.rawOpen
            Layout.fillWidth: true
            text: tool.rawOpen ? "Arguments\n" + tool.pretty(tool.functionData.arguments) +
                  (tool.resultBody ? "\n\nResult\n" + tool.pretty(tool.resultBody.content) : "") : ""
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
            color: tool.dark ? "#b6c7d5" : "#435969"
            font.family: "monospace"
            font.pixelSize: 12
        }
    }
}
