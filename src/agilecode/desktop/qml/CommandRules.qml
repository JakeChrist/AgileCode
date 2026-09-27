pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: editor
    property var rules: []
    property bool dark: true
    property color ink: dark ? "#e3ebf3" : "#223647"
    property color muted: dark ? "#94aabe" : "#607789"
    spacing: 8

    function updateRule(index, field, value) {
        var updated = JSON.parse(JSON.stringify(rules));
        updated[index][field] = value;
        rules = updated;
    }
    function moveRule(index, direction) {
        var updated = rules.slice();
        var target = index + direction;
        if (target < 0 || target >= updated.length) return;
        var rule = updated.splice(index, 1)[0];
        updated.splice(target, 0, rule);
        rules = updated;
    }
    function removeRule(index) {
        var updated = rules.slice();
        updated.splice(index, 1);
        rules = updated;
    }

    Text { text: "Command permissions"; color: editor.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
    Text {
        Layout.fillWidth: true
        text: "First matching pattern wins. Patterns match the entire command, including compound commands; * matches any text. With no match, the run_command tool rule or autonomy setting applies."
        wrapMode: Text.WordWrap; color: editor.muted; font.pixelSize: 12
    }
    Repeater {
        model: editor.rules
        delegate: RowLayout {
            id: row
            required property int index
            required property var modelData
            Layout.fillWidth: true
            Input {
                objectName: "commandRulePattern" + row.index
                Layout.fillWidth: true; dark: editor.dark
                placeholderText: "e.g. pip install *"
                text: row.modelData.pattern
                onEditingFinished: if (text !== row.modelData.pattern) editor.updateRule(row.index, "pattern", text)
            }
            ComboBox {
                objectName: "commandRuleAction" + row.index
                model: ["Allow", "Ask", "Deny"]
                currentIndex: ["allow", "ask", "deny"].indexOf(row.modelData.action)
                onActivated: editor.updateRule(row.index, "action", ["allow", "ask", "deny"][currentIndex])
            }
            ActionButton { text: "↑"; dark: editor.dark; enabled: row.index > 0; onClicked: editor.moveRule(row.index, -1) }
            ActionButton { text: "↓"; dark: editor.dark; enabled: row.index < editor.rules.length - 1; onClicked: editor.moveRule(row.index, 1) }
            ActionButton { text: "Remove"; dark: editor.dark; onClicked: editor.removeRule(row.index) }
        }
    }
    ActionButton {
        objectName: "addCommandRule"
        text: "Add command rule"; dark: editor.dark
        onClicked: editor.rules = editor.rules.concat([{pattern: "", action: "ask"}])
    }
}
