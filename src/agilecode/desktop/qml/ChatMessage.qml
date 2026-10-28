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
    // True only when local GUI input provenance identifies this as human-authored.
    property bool locallyAuthored: false
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
             : message.locallyAuthored && message.role === "user"
               ? (message.dark ? "#22483f" : "#e2f3eb")
               : (message.dark ? "#192936" : "#ffffff")
        border.color: message.navigationHighlighted ? (message.dark ? "#70bda0" : "#247655")
                      : message.locallyAuthored && message.role === "user"
                        ? (message.dark ? "#3b7965" : "#abd4bf")
                        : (message.dark ? "#2b4152" : "#d9e2ea")
    }

    ColumnLayout {
        id: messageColumn
        x: 16; y: 16; width: Math.max(0, parent.width - 32)
        spacing: 9
        // Protocol roles are not author identities. Only transient delivery /
        // interruption statuses need visible text; normal messages are unlabeled.
        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            visible: statusText.text.length > 0 || message.draftRecoverable || message.deletable
            Text {
                id: statusText
                objectName: "messageRole"
                text: {
                    if (message.role === "error") return "Error";
                    if (message.role === "user" && message.locallyAuthored) {
                        if (message.submissionStatus === "sending") return "Sending…";
                        if (message.submissionStatus === "failed") return "Not sent";
                        if (message.submissionStatus === "unknown") return "Send status unknown";
                        if (message.submissionStatus === "queued") return "Queued";
                    }
                    return message.partial ? "Interrupted response" : "";
                }
                visible: text.length > 0
                textFormat: Text.PlainText
                color: message.role === "error" ? (message.dark ? "#ff918e" : "#ad3434")
                     : (message.dark ? "#a6b8c8" : "#526779")
                font.pixelSize: 11
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
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
