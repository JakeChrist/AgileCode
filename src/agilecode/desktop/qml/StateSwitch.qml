import QtQuick
import QtQuick.Controls

Switch {
    id: control
    property string label: ""
    property bool modified: false
    readonly property bool darkAppearance: palette.window.hslLightness < 0.5
    text: label + ": " + (checked ? "On" : "Off") + (modified ? " (unsaved)" : "")
    Accessible.name: text
    indicator: Rectangle {
        implicitWidth: 52
        implicitHeight: 26
        x: control.leftPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        radius: height / 2
        color: control.checked ? (control.darkAppearance ? "#8ad8bd" : "#247655")
                               : (control.darkAppearance ? "#465d70" : "#d2dce5")
        border.color: control.activeFocus ? (control.darkAppearance ? "#e3fff5" : "#123e2c")
                                          : (control.darkAppearance ? "#7f96a8" : "#7c91a2")
        border.width: control.activeFocus ? 2 : 1
        opacity: control.enabled ? 1 : 0.45
        Rectangle {
            width: 20; height: 20; radius: 10
            x: 3 + control.visualPosition * (parent.width - width - 6)
            y: (parent.height - height) / 2
            color: control.checked ? (control.darkAppearance ? "#163b2f" : "#ffffff")
                                   : (control.darkAppearance ? "#f1f6fa" : "#42586a")
            Behavior on x { NumberAnimation { duration: 120 } }
        }
    }
}
