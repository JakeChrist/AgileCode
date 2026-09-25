import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: executionSettings
    property bool dark: true
    property string validationError: ""
    readonly property color ink: dark ? "#e3ebf3" : "#223647"
    readonly property color muted: dark ? "#94aabe" : "#607789"
    spacing: 10

    function load(settings) {
        var profile = settings.profiles[settings.active_profile];
        correctionLimit.text = settings.max_corrections === null ? "" : String(settings.max_corrections === undefined ? 10 : settings.max_corrections);
        modelTimeout.text = profile.timeout_seconds === null || profile.timeout_seconds === undefined ? "" : String(profile.timeout_seconds);
        commandTimeout.text = settings.shell_command_timeout_seconds === null || settings.shell_command_timeout_seconds === undefined ? "" : String(settings.shell_command_timeout_seconds);
        validationError = "";
    }

    function apply(settings) {
        var corrections = correctionLimit.text.trim();
        var model = modelTimeout.text.trim();
        var command = commandTimeout.text.trim();
        if (corrections && (!/^\d+$/.test(corrections) || !Number.isSafeInteger(Number(corrections)))) {
            validationError = "Correction attempts must be a nonnegative whole number, or blank for unlimited.";
            return false;
        }
        if ([model, command].some(function(value) { return value && (!isFinite(Number(value)) || Number(value) <= 0); })) {
            validationError = "Timeouts must be positive seconds, or blank for no timeout.";
            return false;
        }
        settings.max_corrections = corrections ? Number(corrections) : null;
        settings.profiles[settings.active_profile].timeout_seconds = model ? Number(model) : null;
        settings.shell_command_timeout_seconds = command ? Number(command) : null;
        validationError = "";
        return true;
    }

    Text {
        text: "Execution settings"
        color: executionSettings.ink
        font.pixelSize: 18
        font.weight: Font.DemiBold
    }
    GridLayout {
        columns: 2
        Layout.fillWidth: true
        columnSpacing: 18
        rowSpacing: 11
        Text { text: "Correction attempts"; color: executionSettings.muted; Layout.preferredWidth: 150 }
        Input {
            id: correctionLimit
            objectName: "correctionLimitField"
            dark: executionSettings.dark
            Layout.fillWidth: true
            placeholderText: "Unlimited when blank; default 10"
            inputMethodHints: Qt.ImhDigitsOnly
        }
        Text { text: "Model timeout (seconds)"; color: executionSettings.muted; wrapMode: Text.WordWrap; Layout.preferredWidth: 150 }
        Input {
            id: modelTimeout
            objectName: "modelTimeoutField"
            dark: executionSettings.dark
            Layout.fillWidth: true
            placeholderText: "No timeout"
            inputMethodHints: Qt.ImhFormattedNumbersOnly
        }
        Text { text: "Command timeout (seconds)"; color: executionSettings.muted; wrapMode: Text.WordWrap; Layout.preferredWidth: 150 }
        Input {
            id: commandTimeout
            objectName: "commandTimeoutField"
            dark: executionSettings.dark
            Layout.fillWidth: true
            placeholderText: "No timeout"
            inputMethodHints: Qt.ImhFormattedNumbersOnly
        }
    }
    Text {
        Layout.fillWidth: true
        text: "There is no model-request count limit. Blank timeouts mean no timeout, not a large hidden value."
        color: executionSettings.muted
        font.pixelSize: 11
        wrapMode: Text.WordWrap
    }
    Text {
        Layout.fillWidth: true
        visible: executionSettings.validationError.length > 0
        text: executionSettings.validationError
        color: executionSettings.dark ? "#e0a78e" : "#a23620"
        font.pixelSize: 12
        wrapMode: Text.WordWrap
    }
}
