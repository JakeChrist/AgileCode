import QtQuick
import QtQuick.Controls

Button {
    id: control
    property bool primary: false
    property bool danger: false
    property bool dark: true
    implicitHeight: 36
    implicitWidth: Math.max(80, label.implicitWidth + 26)
    leftPadding: 13; rightPadding: 13
    Accessible.name: text
    contentItem: Text {
        id: label
        text: control.text
        font.pixelSize: 13
        font.weight: Font.DemiBold
        color: !control.enabled ? (control.dark ? "#647688" : "#9ba4ae") : control.primary ? "#102a25" : control.danger ? (control.dark ? "#e9a2a7" : "#9a293b") : (control.dark ? "#dfE8F1" : "#263746")
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        radius: 7
        color: control.primary ? (control.down ? "#61b89f" : "#8ad8bd") : (control.hovered ? (control.dark ? "#2d4050" : "#e2e9f0") : (control.dark ? "#20303e" : "#edf2f6"))
        border.color: control.activeFocus ? (control.dark ? "#9ce4cf" : "#247655") : control.primary ? "transparent" : (control.dark ? "#344959" : "#cdd8e2")
        opacity: control.enabled ? 1 : 0.55
    }
}
