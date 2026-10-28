pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Complete presentation of a tool payload. Objects become labeled sections,
// arrays become numbered entries, and every leaf stays selectable. There is no
// second disclosure, clipping limit, allowlist, or serialized-JSON fallback.
ColumnLayout {
    id: fields
    property var payload
    property var inputPayload: ({})
    property string area: "inputs"
    property string toolName: ""
    property bool dark: true
    property bool commandShown: false
    property bool commandOutputShown: false
    readonly property var rows: flatten(payload)
    spacing: 5

    function labelFor(key, path) {
        if (key === "") return "(empty key)";
        var labels = {
            cwd: "Working directory", exit_code: "Exit code", _execution_id: "Execution ID",
            artifact_id: "Artifact ID", job_id: "Job ID", epic_id: "Epic ID", ticket_id: "Ticket ID",
            id: "ID", data: "Returned data", content: "Contents", old: "Original text", new: "Replacement text",
            before_hash: "Previous hash", expected_hash: "Expected hash", cancelled_by_user: "Cancelled by user"
        };
        if (Object.prototype.hasOwnProperty.call(labels, key)) return labels[key];
        var label = key.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
        return label.charAt(0).toUpperCase() + label.slice(1);
    }
    function pointer(path) {
        return path.map(function(key) { return String(key).replace(/~/g, "~0").replace(/\//g, "~1"); }).join("/");
    }
    function proseField(path) {
        var key = path[path.length - 1];
        if (area === "inputs" && path.length === 1) {
            if (toolName === "finish_step") return key === "summary" || key === "output";
            if (toolName === "ask_user") return key === "question";
            if (toolName === "update_progress") return key === "notes";
            if (toolName === "record_check") return key === "method";
        }
        // Ticket and epic descriptions are authored Markdown. File contents,
        // shell output, MCP results and unknown values are always literal text.
        var ticketTool = ["create_ticket", "edit_ticket", "get_ticket", "list_tickets", "create_epic", "update_epic", "get_epic", "list_epics"].indexOf(toolName) >= 0;
        if (ticketTool && (key === "description" || key === "completion_criteria")) return true;
        return area === "result" && toolName === "finish_step" && path.length === 2 && path[0] === "data" && (key === "summary" || key === "output");
    }
    function widgetName(path, prose) {
        var key = path[path.length - 1];
        if (area === "inputs" && prose && path.length === 1) {
            if (toolName === "finish_step" && key === "summary") return "toolProseSummary";
            return "toolProseOutput";
        }
        if (area === "result" && path.length === 2 && path[0] === "data" && key === "output" &&
                (toolName === "run_command" || toolName === "git_command")) return "shellCommandOutput";
        return "toolValue-" + area + "/" + pointer(path);
    }
    function fullTextField(path) {
        var key = path[path.length - 1];
        if (area === "result" && path.length === 2 && path[0] === "data" &&
                ["read_file", "read_artifact", "read_job_output"].indexOf(toolName) >= 0 &&
                (key === "content" || key === "output")) return true;
        if (area === "inputs" && toolName === "write_file" && key === "content") return true;
        if (area === "inputs" && toolName === "edit_file" &&
                (key === "old" || key === "new")) return true;
        return false;
    }
    function describeWhitespace(value) {
        var runs = [];
        for (var i = 0; i < value.length; ++i) {
            var character = value.charAt(i);
            var label = character === "\r" ? "CR" : character === "\n" ? "LF" : character === "\t" ? "Tab" : character === " " ? "Space" : "U+" + value.charCodeAt(i).toString(16).toUpperCase();
            if (runs.length && runs[runs.length - 1].label === label) runs[runs.length - 1].count += 1;
            else runs.push({label: label, count: 1});
        }
        return runs.map(function(run) { return run.label + (run.count > 1 ? " ×" + run.count : ""); }).join(" ");
    }
    function flatten(value) {
        var result = [];
        // Iterative traversal also supports deeply nested extension/MCP data.
        var stack = [{value: value, path: [], label: "", depth: 0}];
        while (stack.length) {
            var node = stack.pop();
            var current = node.value;
            var isArray = Array.isArray(current);
            var isObject = current !== null && typeof current === "object";
            var keys = isObject ? Object.keys(current) : [];
            if (isObject && keys.length) {
                if (node.path.length) {
                    var heading = node.label;
                    // Common search and file-list entries remain complete, but
                    // show their file and line in the section heading.
                    if (area === "result" && node.path.length === 3 && node.path[0] === "data" &&
                            typeof current.path === "string") {
                        if (toolName === "code_search" && node.path[1] === "matches")
                            heading = current.path + (current.start_line != null ? " · line " + current.start_line : "");
                        else if (toolName === "list_files" && node.path[1] === "files")
                            heading = (current.directory ? "Folder · " : "File · ") + current.path;
                    }
                    result.push({path: node.path, label: heading + (isArray ? " (" + current.length + ")" : ""),
                        depth: node.depth, section: true, prose: false, text: "", name: ""});
                }
                for (var index = keys.length - 1; index >= 0; --index) {
                    var key = keys[index];
                    if (commandShown && node.path.length === 0 && key === "command") continue;
                    if (commandOutputShown && area === "result" && node.path.length === 1 &&
                            node.path[0] === "data" && key === "output") continue;
                    var path = node.path.concat([key]);
                    stack.push({value: current[key], path: path,
                        label: isArray ? "Item " + (Number(key) + 1) : labelFor(key, path),
                        depth: node.path.length ? node.depth + 1 : 0});
                }
                continue;
            }
            var text;
            if (current === null) text = "null";
            else if (current === undefined) text = "Not supplied";
            else if (isArray) text = "Empty list";
            else if (isObject) text = "Empty object";
            else if (typeof current === "string" && current.length === 0) text = "Empty text";
            else text = String(current);
            var prose = typeof current === "string" && current.length > 0 && proseField(node.path);
            // finish_step returns its submitted prose unchanged. State the
            // equality explicitly without repeating the full visible passage.
            var echoed = area === "result" && toolName === "finish_step" && node.path.length === 2 && node.path[0] === "data" &&
                (node.path[1] === "summary" || node.path[1] === "output") && typeof current === "string" && current.length > 0 &&
                inputPayload !== null && typeof inputPayload === "object" && inputPayload[node.path[1]] === current;
            if (echoed) { text = "Same as input " + labelFor(node.path[1], node.path) + " (shown above)"; prose = false; }
            result.push({path: node.path, label: node.label, depth: node.depth, section: false, prose: prose,
                fullText: typeof current === "string" && current.length > 0 && !prose && fullTextField(node.path),
                text: text, name: widgetName(node.path, prose),
                whitespace: typeof current === "string" && current.length > 0 && current.trim().length === 0,
                characterCount: typeof current === "string" ? current.length : 0,
                whitespaceText: typeof current === "string" && current.trim().length === 0 ? describeWhitespace(current) : ""});
        }
        return result;
    }

    Repeater {
        model: fields.rows
        delegate: ColumnLayout {
            id: field
            required property var modelData
            readonly property bool quietMetadata: ["Expected hash", "Previous hash", "Hash", "Execution ID",
                                                   "Bytes", "Size", "ID", "Job ID", "Artifact ID"].indexOf(modelData.label) >= 0
            Layout.fillWidth: true
            // Keep even very deep values readable in narrow windows. Their
            // full path is shown once indentation reaches its visual limit.
            Layout.leftMargin: Math.min(modelData.depth, 6) * 12
            spacing: 2
            Rectangle {
                visible: field.modelData.section && field.modelData.depth <= 2
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: fields.dark ? "#344959" : "#d9e2ea"
            }
            TextEdit {
                objectName: "toolLabel-" + fields.area + "/" + fields.pointer(field.modelData.path)
                visible: field.modelData.label.length > 0 && !field.modelData.fullText
                Layout.fillWidth: true
                text: (field.modelData.depth > 6 ? field.modelData.path.map(function(key) { return fields.labelFor(String(key), []); }).join(" › ") : field.modelData.label) + ":"
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                color: field.modelData.section ? (fields.dark ? "#d5e2eb" : "#223647") :
                       (field.quietMetadata ? (fields.dark ? "#849aaa" : "#718394") :
                        (fields.dark ? "#a6b8c8" : "#526779"))
                font.pixelSize: field.modelData.section ? 12 : 11
                font.weight: Font.DemiBold
            }
            TextEdit {
                objectName: field.modelData.prose ? "" : field.modelData.name
                property var fieldPath: field.modelData.path
                property string sourceText: field.modelData.text
                visible: !field.modelData.section && !field.modelData.prose && !field.modelData.fullText
                Layout.fillWidth: true
                text: visible ? field.modelData.text : ""
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                color: field.quietMetadata ? (fields.dark ? "#9eb1be" : "#607789")
                       : (fields.dark ? "#d5e2eb" : "#223647")
                font.family: "monospace"
                font.pixelSize: field.quietMetadata ? 11 : 12
            }
            Loader {
                active: !!field.modelData.fullText
                visible: active
                Layout.fillWidth: true
                sourceComponent: Component {
                    ToolTextPanel {
                        objectName: "completeToolField"
                        title: fields.toolName === "read_file" && fields.area === "result" && fields.payload && fields.payload.data
                              ? "File: " + String(fields.payload.data.path || "") +
                                (fields.payload.data.start_line != null ? " · lines " + fields.payload.data.start_line +
                                 "–" + fields.payload.data.end_line : "")
                              : (field.modelData.label || "Complete text")
                        inlineObjectName: field.modelData.name
                        sourceText: field.modelData.text
                        dark: fields.dark
                    }
                }
            }
            ToolProse {
                objectName: field.modelData.prose ? field.modelData.name : ""
                property var fieldPath: field.modelData.path
                visible: !field.modelData.section && field.modelData.prose
                Layout.fillWidth: true
                sourceText: visible ? field.modelData.text : ""
                dark: fields.dark
            }
            TextEdit {
                visible: !!field.modelData.whitespace
                Layout.fillWidth: true
                text: "Whitespace only: " + (field.modelData.whitespaceText || "") + " (" + field.modelData.characterCount + " characters)"
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                color: fields.dark ? "#a6b8c8" : "#526779"
                font.pixelSize: 11
            }
        }
    }
}
