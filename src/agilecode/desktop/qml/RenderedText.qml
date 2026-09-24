import QtQuick
import QtQuick.Controls
import AgileCode.Desktop 1.0

TextArea {
    id: rendered
    property string sourceText: ""
    property bool dark: true
    property bool streaming: false
    property bool partial: false

    function cleanStreamingMarkdown(value) {
        var text = String(value || "");
        text = text.replace(/\r/g, "");
        text = text.replace(/^\s*#+\s+/gm, "");
        text = text.replace(/^\s*>\s?/gm, "");
        text = text.replace(/^\s*[-+*]\s+/gm, "• ");
        text = text.replace(/^\s*```[^\n]*$/gm, "");
        text = text.replace(/```/g, "");
        text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
        text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
        text = text.replace(/\*\*([^*]+)\*\*/g, "$1");
        text = text.replace(/__([^_]+)__/g, "$1");
        text = text.replace(/~~([^~]+)~~/g, "$1");
        text = text.replace(/`([^`]+)`/g, "$1");
        text = text.replace(/(^|[^A-Za-z0-9])\*([^*\n]+)\*/g, "$1$2");
        text = text.replace(/\*\*/g, "");
        text = text.replace(/~~/g, "");
        text = text.replace(/`/g, "");
        text = text.replace(/(^|[\s(])\*(?=\S)/g, "$1");
        text = text.replace(/\*(?=$|[\s).,!?:;])/g, "");
        return text;
    }

    text: (streaming || partial) ? cleanStreamingMarkdown(sourceText) : sourceText
    textFormat: (streaming || partial) ? TextEdit.PlainText : TextEdit.MarkdownText
    readOnly: true
    selectByMouse: true
    wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
    padding: 0
    background: null
    selectionColor: "#406959"
    selectedTextColor: "#ffffff"
    color: dark ? "#e3ebf3" : "#223647"
    font.pixelSize: 14
    onLinkActivated: function(link) { Qt.openUrlExternally(link); }

    ChatDocumentStyle {
        document: (rendered.streaming || rendered.partial) ? null : rendered.textDocument
        dark: rendered.dark
    }
}
