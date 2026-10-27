import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Presentation-only navigation over messages already loaded by ConversationView.
RowLayout {
    id: bar
    property var conversation: null
    property bool dark: true
    spacing: 4
    implicitHeight: 34

    ToolButton {
        text: "↑"
        enabled: !!bar.conversation && bar.conversation.count > 0
        ToolTip.visible: hovered
        ToolTip.text: "Previous message"
        Accessible.name: "Previous message"
        onClicked: bar.conversation.navigateMessage(-1)
    }
    ToolButton {
        text: "↓"
        enabled: !!bar.conversation && bar.conversation.count > 0
        ToolTip.visible: hovered
        ToolTip.text: "Next message"
        Accessible.name: "Next message"
        onClicked: bar.conversation.navigateMessage(1)
    }
    Rectangle {
        Layout.preferredWidth: 1
        Layout.preferredHeight: 20
        color: bar.dark ? "#344959" : "#d9e2ea"
    }
    TextField {
        id: findInput
        objectName: "conversationFindInput"
        Layout.fillWidth: true
        Layout.minimumWidth: 105
        Layout.maximumWidth: 340
        implicitHeight: 32
        font.pixelSize: 12
        color: bar.dark ? "#e3ebf3" : "#223647"
        placeholderText: "Find in loaded messages and tools…"
        placeholderTextColor: bar.dark ? "#94aabe" : "#607789"
        selectByMouse: true
        background: Rectangle {
            color: bar.dark ? "#101c27" : "#ffffff"
            radius: 6
            border.color: findInput.activeFocus ? (bar.dark ? "#8ad8bd" : "#247655")
                                                : (bar.dark ? "#344959" : "#d9e2ea")
        }
        onTextChanged: if (bar.conversation) bar.conversation.findTerm = text
        onAccepted: if (bar.conversation) bar.conversation.navigateFind(1)
    }
    ToolButton {
        text: "‹"
        enabled: !!bar.conversation && bar.conversation.findMatches.length > 0
        ToolTip.visible: hovered
        ToolTip.text: "Previous found message"
        Accessible.name: "Previous search match"
        onClicked: bar.conversation.navigateFind(-1)
    }
    ToolButton {
        text: "›"
        enabled: !!bar.conversation && bar.conversation.findMatches.length > 0
        ToolTip.visible: hovered
        ToolTip.text: "Next found message"
        Accessible.name: "Next search match"
        onClicked: bar.conversation.navigateFind(1)
    }
    Text {
        visible: findInput.text.length > 0
        text: bar.conversation ? (bar.conversation.findCursor < 0 ? 0 : bar.conversation.findCursor + 1) +
              "/" + bar.conversation.findMatches.length : "0/0"
        color: bar.dark ? "#94aabe" : "#607789"
        font.pixelSize: 11
    }
    Item { Layout.fillWidth: true }
    ToolButton {
        text: "Latest"
        visible: !!bar.conversation && !bar.conversation.followTail
        enabled: !!bar.conversation
        ToolTip.visible: hovered
        ToolTip.text: "Return to latest message and follow new output"
        Accessible.name: "Latest message"
        onClicked: bar.conversation.jumpToTail()
    }
}
