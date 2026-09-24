import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: reasoning
    property string content: ""
    property bool streaming: false
    property bool dark: true
    property bool expanded: false
    signal userToggled(bool expanded)
    spacing: 6

    ToolButton {
        objectName: "reasoningToggle"
        text: (reasoning.expanded ? "▾" : "▸") + " Reasoning" + (reasoning.streaming ? " · thinking…" : "")
        checkable: true
        checked: reasoning.expanded
        onClicked: {
            reasoning.expanded = checked
            reasoning.userToggled(reasoning.expanded)
        }
        font.pixelSize: 12
        palette.buttonText: reasoning.dark ? "#a6b8c8" : "#526779"
        Accessible.name: "Reasoning"
        Accessible.description: reasoning.expanded ? "Collapse returned reasoning" : "Expand returned reasoning"
    }
    TextEdit {
        objectName: "reasoningText"
        visible: reasoning.expanded
        Layout.fillWidth: true
        Layout.leftMargin: 12
        text: reasoning.content
        textFormat: TextEdit.PlainText
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.Wrap
        color: reasoning.dark ? "#a6b8c8" : "#526779"
        selectionColor: "#406959"
        selectedTextColor: "#ffffff"
        font.pixelSize: 13
    }
}
