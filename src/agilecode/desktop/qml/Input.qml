import QtQuick
import QtQuick.Controls

TextField {
    id: control
    property bool dark: true
    implicitHeight: 39
    leftPadding: 12; rightPadding: 12
    font.pixelSize: 14
    color: dark ? "#e1e8ef" : "#253748"
    placeholderTextColor: dark ? "#8195a8" : "#65798c"
    selectionColor: "#396858"
    background: Rectangle {
        color: control.dark ? "#101c27" : "white"
        radius: 7
        border.color: control.activeFocus ? "#8ad8bd" : (control.dark ? "#34495b" : "#ccd6df")
    }
}
