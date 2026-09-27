import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: reasoning
    property string content: ""
    property bool streaming: false
    property bool dark: true
    property bool defaultExpanded: false
    property bool expanded: defaultExpanded
    signal userToggled(bool expanded)
    signal toggling()
    spacing: 6

    ToolButton {
        objectName: "reasoningToggle"
        text: (reasoning.expanded ? "▾" : "▸") + " Reasoning" + (reasoning.streaming ? " · thinking…" : "")
        checkable: true
        checked: reasoning.expanded
        onClicked: {
            reasoning.toggling();
            reasoning.expanded = checked
            reasoning.userToggled(reasoning.expanded)
        }
        font.pixelSize: 12
        palette.buttonText: reasoning.dark ? "#a6b8c8" : "#526779"
        Accessible.name: "Reasoning"
        Accessible.description: reasoning.expanded ? "Collapse returned reasoning" : "Expand returned reasoning"
    }
    RenderedText {
        objectName: "reasoningText"
        visible: reasoning.expanded
        Layout.fillWidth: true
        Layout.leftMargin: 12
        sourceText: reasoning.content
        streaming: reasoning.streaming
        dark: reasoning.dark
        color: reasoning.dark ? "#a6b8c8" : "#526779"
        font.pixelSize: 13
    }
}
