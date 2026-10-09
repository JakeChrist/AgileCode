import QtQuick
import QtQuick.Controls

Item {
    id: diffView
    property string sourceText: ""
    property bool dark: true
    property bool loading: false
    property string error: ""
    property bool changed: false

    // Use the same addition/removal background palette as the existing CodePane.
    readonly property color baseColor: dark ? "#101820" : "#ffffff"
    readonly property color gutterColor: dark ? "#16212b" : "#f1f4f8"
    readonly property color ink: dark ? "#d8e2ed" : "#233144"
    readonly property color muted: dark ? "#8298ac" : "#61788e"
    readonly property var parsed: parseDiff(sourceText)
    readonly property var rows: parsed.rows
    readonly property int maxColumns: parsed.columns
    implicitHeight: loading || error.length > 0 || !changed || rows.length === 0
                    ? 70 : Math.max(70, Math.min(400, rows.length * 22 + 2))

    function parseDiff(source) {
        var result = [];
        var longest = 0;
        var oldLine = 0;
        var newLine = 0;
        var inHunk = false;
        var raw = String(source || "").replace(/\r/g, "").split("\n");
        if (raw.length && raw[raw.length - 1] === "") raw.pop();
        for (var i = 0; i < raw.length; i++) {
            var line = raw[i];
            var kind = "meta";
            var oldNumber = "";
            var newNumber = "";
            if (line.startsWith("diff --git ")) {
                kind = "file";
                inHunk = false;
            } else if (line.startsWith("@@")) {
                kind = "hunk";
                var header = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
                if (header) {
                    oldLine = Number(header[1]);
                    newLine = Number(header[2]);
                    inHunk = true;
                } else {
                    inHunk = false;
                }
            } else if (line.startsWith("+++") || line.startsWith("---") || !inHunk) {
                kind = "meta";
            } else if (line.startsWith("+")) {
                kind = "add";
                newNumber = newLine++;
            } else if (line.startsWith("-")) {
                kind = "remove";
                oldNumber = oldLine++;
            } else if (line.startsWith(" ")) {
                kind = "context";
                oldNumber = oldLine++;
                newNumber = newLine++;
            } else {
                // Includes the Git marker for a missing final newline.
                kind = "meta";
            }
            result.push({kind: kind, text: line, oldNumber: oldNumber, newNumber: newNumber});
            longest = Math.max(longest, line.length);
        }
        return {rows: result, columns: longest};
    }

    function backgroundFor(kind) {
        if (kind === "add") return dark ? "#183e32" : "#dff2e6";
        if (kind === "remove") return dark ? "#492b32" : "#f9e3e5";
        if (kind === "file") return dark ? "#263f54" : "#dce9f6";
        if (kind === "hunk") return dark ? "#203849" : "#eaf2fa";
        return baseColor;
    }

    function foregroundFor(kind) {
        if (kind === "add") return dark ? "#9be1bc" : "#206544";
        if (kind === "remove") return dark ? "#f2a6b0" : "#963340";
        if (kind === "file" || kind === "hunk") return dark ? "#a9d4f5" : "#28587a";
        if (kind === "meta") return muted;
        return ink;
    }

    Rectangle {
        anchors.fill: parent
        radius: 6
        color: diffView.baseColor
        border.color: diffView.dark ? "#344959" : "#d9e2ea"
        clip: true

        Text {
            anchors.centerIn: parent
            visible: diffView.loading || diffView.error.length > 0 || !diffView.changed || diffView.rows.length === 0
            text: diffView.loading ? "Loading diff…" :
                  diffView.error.length ? "Step diff unavailable: " + diffView.error :
                  !diffView.changed ? "No file changes." : "No textual diff available."
            color: diffView.muted
            font.pixelSize: 12
            width: parent.width - 24
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }

        ListView {
            id: linesView
            anchors.fill: parent
            anchors.margins: 1
            visible: !diffView.loading && diffView.error.length === 0 && diffView.changed && diffView.rows.length > 0
            clip: true
            model: visible ? diffView.rows : []
            spacing: 0
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.AutoFlickDirection
            contentWidth: Math.max(width, diffView.maxColumns * 8 + 125)
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded }

            delegate: Rectangle {
                id: diffLine
                required property var modelData
                readonly property var entry: modelData
                width: linesView.contentWidth
                height: 22
                color: diffView.backgroundFor(entry.kind)

                Row {
                    anchors.fill: parent
                    spacing: 0

                    Rectangle {
                        width: 42
                        height: parent.height
                        color: diffView.gutterColor
                        Text {
                            anchors.right: parent.right
                            anchors.rightMargin: 5
                            anchors.verticalCenter: parent.verticalCenter
                            text: diffLine.entry.oldNumber
                            color: diffView.muted
                            font.family: "Monospace"
                            font.pixelSize: 11
                        }
                    }
                    Rectangle {
                        width: 42
                        height: parent.height
                        color: diffView.gutterColor
                        Text {
                            anchors.right: parent.right
                            anchors.rightMargin: 5
                            anchors.verticalCenter: parent.verticalCenter
                            text: diffLine.entry.newNumber
                            color: diffView.muted
                            font.family: "Monospace"
                            font.pixelSize: 11
                        }
                    }
                    Rectangle {
                        width: 3
                        height: parent.height
                        color: diffLine.entry.kind === "add" ? (diffView.dark ? "#58b78e" : "#389363")
                             : diffLine.entry.kind === "remove" ? (diffView.dark ? "#d27b8b" : "#c85b6c")
                             : "transparent"
                    }
                    TextEdit {
                        width: Math.max(0, diffLine.width - 87)
                        height: parent.height
                        text: diffLine.entry.text
                        textFormat: TextEdit.PlainText
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.NoWrap
                        verticalAlignment: TextEdit.AlignVCenter
                        padding: 0
                        leftPadding: 7
                        color: diffView.foregroundFor(diffLine.entry.kind)
                        font.family: "Monospace"
                        font.pixelSize: 12
                        font.weight: diffLine.entry.kind === "file" ? Font.DemiBold : Font.Normal
                    }
                }
            }
        }
    }
}
