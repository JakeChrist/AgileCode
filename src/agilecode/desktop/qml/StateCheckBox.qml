import QtQuick
import QtQuick.Controls

CheckBox {
    id: control
    property string label: ""
    property bool modified: false
    text: label + ": " + (checked ? "On" : "Off") + (modified ? " (unsaved)" : "")
    Accessible.name: text
}
