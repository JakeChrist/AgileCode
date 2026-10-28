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
    onDefaultExpandedChanged: expanded = defaultExpanded
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
        font.weight: Font.DemiBold
        palette.buttonText: reasoning.dark ? "#b4c7d5" : "#526779"
        background: Rectangle {
            radius: 5
            color: reasoning.dark ? "#203442" : "#e8f0f5"
            border.color: reasoning.dark ? "#334a5b" : "#ccdce4"
        }
        Accessible.name: "Reasoning"
        Accessible.description: reasoning.expanded ? "Collapse returned reasoning" : "Expand returned reasoning"
    }
    Rectangle {
        id: reasoningSurface
        objectName: "reasoningSurface"
        visible: reasoning.expanded
        Layout.fillWidth: true
        implicitHeight: reasoningText.implicitHeight + 24
        radius: 6
        color: reasoning.dark ? "#14242f" : "#f3f7fa"
        border.color: reasoning.dark ? "#324a59" : "#d2e0e8"
        RenderedText {
            id: reasoningText
            objectName: "reasoningText"
            anchors.fill: parent
            anchors.margins: 12
            sourceText: reasoning.content
            streaming: reasoning.streaming
            dark: reasoning.dark
            color: reasoning.dark ? "#9cb1c3" : "#536b7e"
            font.pixelSize: 13
        }
    }
}
