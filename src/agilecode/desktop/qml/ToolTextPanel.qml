import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A complete, literal tool value in a bounded inline viewer and optional large viewer.
// The original text is never modified, truncated or sent back to the backend.
ColumnLayout {
    id: panel
    property string title: "Output"
    property string sourceText: ""
    property string inlineObjectName: ""
    property bool dark: true
    property bool wrapLines: true
    property int maxInlineHeight: 220
    readonly property real unwrappedWidth: sourceText.split("\n").reduce(function(best, line) {
        return Math.max(best, line.length);
    }, 0) * 8 + 24
    spacing: 5
    Layout.fillWidth: true

    function copyAll() {
        // TextArea's native clipboard action copies the original complete value.
        inlineText.selectAll();
        inlineText.copy();
        inlineText.deselect();
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            text: panel.title
            color: panel.dark ? "#a6b8c8" : "#526779"
            font.pixelSize: 12
            font.weight: Font.DemiBold
            Layout.fillWidth: true
        }
        ToolButton {
            text: "⋯"
            font.pixelSize: 16
            Accessible.name: panel.title + " actions"
            ToolTip.visible: hovered
            ToolTip.text: "Copy, wrap, or open complete text"
            onClicked: outputActions.open()
            Menu {
                id: outputActions
                y: parent.height
                MenuItem { text: "Copy"; onTriggered: panel.copyAll() }
                MenuItem {
                    text: panel.wrapLines ? "Unwrap lines" : "Wrap lines"
                    onTriggered: panel.wrapLines = !panel.wrapLines
                }
                MenuItem { text: "Open full"; onTriggered: fullDialog.open() }
            }
        }
    }
    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(panel.maxInlineHeight,
            Math.max(48, Math.max(panel.sourceText.split("\n").length * 19,
                                   panel.sourceText.length / 95 * 18) + 12))
        color: panel.dark ? "#101820" : "#f7f9fb"
        radius: 6
        border.color: panel.dark ? "#344959" : "#d9e2ea"
        clip: true
        ScrollView {
            id: inlineScroll
            anchors.fill: parent
            anchors.margins: 5
            contentWidth: panel.wrapLines ? availableWidth : Math.max(availableWidth, panel.unwrappedWidth)
            ScrollBar.horizontal.policy: panel.wrapLines ? ScrollBar.AlwaysOff : ScrollBar.AsNeeded
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            TextArea {
                id: inlineText
                objectName: panel.inlineObjectName
                width: panel.wrapLines ? inlineScroll.availableWidth : Math.max(inlineScroll.availableWidth, panel.unwrappedWidth)
                text: panel.sourceText
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: panel.wrapLines ? TextEdit.WrapAtWordBoundaryOrAnywhere : TextEdit.NoWrap
                padding: 5
                background: null
                color: panel.dark ? "#d5e2eb" : "#223647"
                font.family: "monospace"
                font.pixelSize: 12
            }
        }
    }

    Dialog {
        id: fullDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(1100, parent ? parent.width - 60 : 900)
        height: Math.min(800, parent ? parent.height - 60 : 700)
        modal: true
        title: panel.title + " — complete text"
        padding: 12
        contentItem: ColumnLayout {
            spacing: 8
            ScrollView {
                id: fullScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: panel.wrapLines ? availableWidth : Math.max(availableWidth, panel.unwrappedWidth)
                ScrollBar.horizontal.policy: panel.wrapLines ? ScrollBar.AlwaysOff : ScrollBar.AsNeeded
                ScrollBar.vertical.policy: ScrollBar.AsNeeded
                TextArea {
                    id: fullText
                    width: panel.wrapLines ? fullScroll.availableWidth : Math.max(fullScroll.availableWidth, panel.unwrappedWidth)
                    text: fullDialog.opened ? panel.sourceText : ""
                    textFormat: TextEdit.PlainText
                    readOnly: true
                    selectByMouse: true
                    wrapMode: panel.wrapLines ? TextEdit.WrapAtWordBoundaryOrAnywhere : TextEdit.NoWrap
                    background: null
                    color: panel.dark ? "#e3ebf3" : "#223647"
                    font.family: "monospace"
                    font.pixelSize: 13
                }
            }
            RowLayout {
                Layout.fillWidth: true
                ToolButton {
                    text: panel.wrapLines ? "Unwrap lines" : "Wrap lines"
                    onClicked: panel.wrapLines = !panel.wrapLines
                }
                Item { Layout.fillWidth: true }
                ToolButton { text: "Copy all"; onClicked: panel.copyAll() }
                Button { text: "Close"; onClicked: fullDialog.close() }
            }
        }
        background: Rectangle {
            color: panel.dark ? "#16232f" : "#ffffff"
            radius: 8
            border.color: panel.dark ? "#344959" : "#d9e2ea"
        }
    }
}
