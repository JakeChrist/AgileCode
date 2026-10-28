pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import AgileCode.Desktop 1.0

Item {
    id: message
    property var body: ({})
    property var toolResults: ({})
    property bool partial: false
    property bool streaming: false
    property bool deletable: false
    property string submissionStatus: ""
    property bool draftRecoverable: false
    property bool dark: true
    property bool reasoningExpandedByDefault: false
    property bool toolsExpandedByDefault: false
    property var toolExpansionOverrides: ({})
    property string assistantName: "Assistant"
    property bool navigationHighlighted: false
    property string findTerm: ""
    signal reasoningToggled(bool expanded)
    signal toolToggled(string callId, string fallbackId, bool expanded)
    signal disclosureToggling()
    signal deleteRequested()
    signal restoreDraftRequested()
    readonly property string role: body.role || "assistant"
    readonly property string reasoningText: body.reasoning_content || body.reasoning || ""
    implicitHeight: messageColumn.implicitHeight + 32

    function toolExpansionDefault(callId, fallbackId) {
        if (Object.prototype.hasOwnProperty.call(toolExpansionOverrides, callId))
            return !!toolExpansionOverrides[callId];
        // Some streams supply the tool ID after its first argument fragments.
        if (fallbackId && Object.prototype.hasOwnProperty.call(toolExpansionOverrides, fallbackId))
            return !!toolExpansionOverrides[fallbackId];
        return toolsExpandedByDefault;
    }

    function toolForAnchor(key) {
        if (key === "orphan") return message.role === "tool" ? orphanResult : null;
        if (key.indexOf("index:") === 0) return toolCalls.itemAt(Number(key.slice(6)));
        return null;
    }

    function captureToolAnchor(viewportTop, viewportHeight, expanding) {
        var containing = null;
        var count = toolCalls.count + (message.role === "tool" ? 1 : 0);
        for (var i = 0; i < count; ++i) {
            var key = i < toolCalls.count ? "index:" + i : "orphan";
            var item = toolForAnchor(key);
            if (!item) continue;
            var top = item.mapToItem(message, 0, 0).y;
            if (top >= viewportTop && top < viewportTop + viewportHeight)
                return {key: key, offset: top - viewportTop};
            if (top < viewportTop && top + item.height > viewportTop)
                containing = {key: key, offset: expanding ? top - viewportTop : 0};
        }
        // If collapsing removes the text being read, keep its surviving header visible.
        return containing;
    }

    function toolAnchorPosition(key) {
        var item = toolForAnchor(key);
        return item ? item.mapToItem(message, 0, 0).y : null;
    }

    // The same text and message ordering, but with distinct visual surfaces.
    Rectangle {
        id: messageSurface
        objectName: "messageSurface"
        anchors.fill: parent
        visible: message.role !== "tool"
        radius: 8
        color: message.navigationHighlighted ? (message.dark ? "#254a43" : "#e4f2eb")
             : message.role === "user" ? (message.dark ? "#1d3542" : "#edf5f8")
             : (message.dark ? "#192936" : "#ffffff")
        border.color: message.navigationHighlighted ? (message.dark ? "#70bda0" : "#247655")
                      : message.role === "user" ? (message.dark ? "#365666" : "#bdd5df")
                      : (message.dark ? "#2b4152" : "#d9e2ea")
    }

    Rectangle {
        visible: message.role === "user"
        x: 0; y: 14; width: 3; height: Math.max(0, messageColumn.implicitHeight + 4)
        radius: 1
        color: message.dark ? "#8ad8bd" : "#39745c"
    }
    ColumnLayout {
        id: messageColumn
        x: 16; y: 16; width: Math.max(0, parent.width - 32)
        spacing: 9
        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            Text {
                objectName: "messageRole"
                Layout.fillWidth: true
                text: (message.role === "user" ? "You" : (message.role === "assistant" ? message.assistantName : (message.role === "error" ? "Error" : "System"))) + (message.role === "user"
                    ? (message.submissionStatus === "sending" ? " · Sending…" :
                       message.submissionStatus === "failed" ? " · Not sent" :
                       message.submissionStatus === "unknown" ? " · Send status unknown" :
                       message.submissionStatus === "queued" ? " · Queued" : "")
                    : (message.partial ? " · Interrupted response" : (message.streaming ? " · responding…" : "")))
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: message.role === "error" ? (message.dark ? "#ff918e" : "#ad3434")
                     : message.role === "user" ? (message.dark ? "#8ad8bd" : "#266e58")
                     : (message.dark ? "#c0d2e0" : "#40586d")
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
            ToolButton {
                objectName: "restoreMessageDraft"
                text: "Restore draft"
                visible: message.draftRecoverable
                font.pixelSize: 11
                palette.buttonText: message.dark ? "#a6b8c8" : "#526779"
                onClicked: message.restoreDraftRequested()
            }
            ToolButton {
                text: "Delete"
                visible: message.deletable
                font.pixelSize: 11
                palette.buttonText: message.dark ? "#a6b8c8" : "#526779"
                onClicked: message.deleteRequested()
            }
        }
        ReasoningSection {
            objectName: "reasoningSection"
            Layout.fillWidth: true
            visible: message.reasoningText.length > 0
            content: message.reasoningText
            streaming: message.streaming && !message.body.content
            dark: message.dark
            defaultExpanded: message.reasoningExpandedByDefault
            onUserToggled: function(expanded) { message.reasoningToggled(expanded); }
            onToggling: message.disclosureToggling()
        }
        Text {
            objectName: "responseLabel"
            visible: message.role === "assistant" && message.reasoningText.length > 0
                     && (message.body.content || "").length > 0
            text: "Response"
            color: message.dark ? "#9ed7c1" : "#39745c"
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }
        RenderedText {
            id: answerText
            objectName: "answerText"
            Layout.fillWidth: true
            visible: sourceText.length > 0 && message.role !== "tool"
            sourceText: message.body.content || ""
            streaming: message.streaming
            partial: message.partial
            dark: message.dark
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: message.dark ? "#314656" : "#d9e2ea"
            visible: (message.body.tool_calls || []).length > 0 &&
                     ((message.body.content || "").length > 0 || message.reasoningText.length > 0)
        }
        Repeater {
            id: toolCalls
            model: message.body.tool_calls || []
            delegate: ToolCall {
                required property var modelData
                required property int index
                readonly property string expansionKey: modelData.id ? "call:" + modelData.id : "index:" + index
                Layout.fillWidth: true
                callData: modelData
                resultBody: message.toolResults[modelData.id] || null
                streaming: message.streaming
                expandedByDefault: message.toolExpansionDefault(expansionKey, "index:" + index)
                onUserToggled: function(expanded) { message.toolToggled(expansionKey, "index:" + index, expanded); }
                dark: message.dark
                findTerm: message.findTerm
                onToggling: message.disclosureToggling()
            }
        }
        // Tool results outside a loaded assistant page are still inspectable.
        ToolCall {
            id: orphanResult
            Layout.fillWidth: true
            visible: message.role === "tool"
            callData: ({function: {name: "Tool result", arguments: ""}})
            displayLabel: "Result for call " + (message.body.tool_call_id || "(unmatched)")
            resultBody: message.body
            expandedByDefault: message.toolExpansionDefault("call:" + (message.body.tool_call_id || "__orphan__"))
            onUserToggled: function(expanded) { message.toolToggled("call:" + (message.body.tool_call_id || "__orphan__"), "", expanded); }
            dark: message.dark
            findTerm: message.findTerm
            onToggling: message.disclosureToggling()
        }
    }
}
