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
    property bool dark: true
    property string assistantName: "Assistant"
    readonly property string role: body.role || "assistant"
    readonly property string reasoningText: body.reasoning_content || body.reasoning || ""
    implicitHeight: messageColumn.implicitHeight + 24

    // A quiet rule distinguishes user input without turning the transcript into cards.
    Rectangle {
        visible: message.role === "user"
        x: 0; y: 12; width: 2; height: messageColumn.implicitHeight
        color: message.dark ? "#8ad8bd" : "#39745c"
    }
    ColumnLayout {
        id: messageColumn
        x: 12; y: 12; width: Math.max(0, parent.width - 24)
        spacing: 8
        Text {
            objectName: "messageRole"
            Layout.fillWidth: true
            text: (message.role === "user" ? "You" : (message.role === "assistant" ? message.assistantName : "System")) + (message.partial ? " · Interrupted response" : (message.streaming ? " · responding…" : ""))
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: message.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }
        ReasoningSection {
            objectName: "reasoningSection"
            Layout.fillWidth: true
            visible: message.reasoningText.length > 0
            content: message.reasoningText
            streaming: message.streaming && !message.body.content
            dark: message.dark
        }
        TextArea {
            id: answerText
            objectName: "answerText"
            Layout.fillWidth: true
            visible: text.length > 0 && message.role !== "tool"
            text: message.body.content || ""
            textFormat: message.role === "assistant" ? TextEdit.MarkdownText : TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.Wrap
            padding: 0
            background: null
            selectionColor: "#406959"
            selectedTextColor: "#ffffff"
            color: message.dark ? "#e3ebf3" : "#223647"
            font.pixelSize: 14
            onLinkActivated: function(link) { Qt.openUrlExternally(link); }
            ChatDocumentStyle {
                // Reformatting the QTextDocument on every streamed token causes
                // repeated relayout while reasoning and answer sections coexist.
                // Apply the richer block styling once the response is complete.
                document: message.streaming ? null : answerText.textDocument
                dark: message.dark
            }
        }
        Repeater {
            model: message.body.tool_calls || []
            delegate: ToolCall {
                required property var modelData
                Layout.fillWidth: true
                callData: modelData
                resultBody: message.toolResults[modelData.id] || null
                streaming: message.streaming
                dark: message.dark
            }
        }
        // Tool results outside a loaded assistant page are still inspectable.
        ToolCall {
            Layout.fillWidth: true
            visible: message.role === "tool"
            callData: ({function: {name: "Tool result", arguments: ""}})
            resultBody: message.body
            dark: message.dark
        }
    }
}
