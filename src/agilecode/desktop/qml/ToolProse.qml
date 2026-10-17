import QtQuick
import QtQuick.Controls
import AgileCode.Desktop 1.0

// Only selected authored prose uses Markdown. Literal HTML and images remain
// text, and tool-provided links cannot launch applications or execute commands.
TextArea {
    id: prose
    property string sourceText: ""
    property bool dark: true
    function updateText() {
        if (!presentation) return;
        // Set wrapping before loading the document: changing a long document
        // from NoWrap/word-boundary mode can otherwise block Qt's layout.
        text = "";
        wrapMode = /\S{256}/.test(sourceText) ? TextEdit.WrapAnywhere : TextEdit.WrapAtWordBoundaryOrAnywhere;
        text = presentation.toolHtml(sourceText);
    }
    onSourceTextChanged: updateText()
    Component.onCompleted: updateText()
    textFormat: TextEdit.RichText
    readOnly: true
    selectByMouse: true
    wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
    padding: 0
    background: null
    selectionColor: "#406959"
    selectedTextColor: "#ffffff"
    color: dark ? "#d5e2eb" : "#223647"
    font.pixelSize: 12
    MarkdownPresentation { id: presentation }
    ChatDocumentStyle { document: prose.textDocument; dark: prose.dark }
}
