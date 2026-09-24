"""Presentation-only styling of Qt's already parsed Markdown document."""
from PySide6.QtCore import QObject, Property, Signal, QTimer, Slot
from PySide6.QtGui import QColor, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextFormat, QTextDocument
from PySide6.QtQuick import QQuickTextDocument


class ChatDocumentStyle(QObject):
    documentChanged = Signal()
    darkChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._document = None
        self._dark = True
        self._applying = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply)

    @Property(QQuickTextDocument, notify=documentChanged)
    def document(self):
        return self._document

    @document.setter
    def document(self, value):
        if self._document is not None:
            self._document.textDocument().contentsChanged.disconnect(self._schedule)
        self._document = value
        if value is not None:
            value.textDocument().contentsChanged.connect(self._schedule)
        self.documentChanged.emit()
        self._schedule()

    @Property(bool, notify=darkChanged)
    def dark(self):
        return self._dark

    @dark.setter
    def dark(self, value):
        if value != self._dark:
            self._dark = value
            self.darkChanged.emit()
            self._schedule()

    def _schedule(self):
        if not self._applying:
            self._timer.start(0)

    def _apply(self):
        if self._document is None:
            return
        document = self._document.textDocument()
        self._applying = True
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        try:
            block = document.begin()
            while block.isValid():
                fmt = block.blockFormat()
                code = fmt.hasProperty(QTextFormat.BlockCodeFence) or fmt.nonBreakableLines()
                fmt.setTopMargin(2 if code or block.textList() else 8)
                fmt.setBottomMargin(2 if code else 4)
                fmt.setLineHeight(120 if code else 135, QTextBlockFormat.ProportionalHeight.value)
                if code:
                    fmt.setBackground(QColor("#101923" if self._dark else "#edf2f6"))
                    fmt.setLeftMargin(12)
                    fmt.setRightMargin(12)
                cursor.setPosition(block.position())
                cursor.setBlockFormat(fmt)
                if code:
                    code_format = QTextCharFormat()
                    code_format.setBackground(QColor("#101923" if self._dark else "#edf2f6"))
                    cursor.setPosition(block.position() + block.length() - 1, QTextCursor.KeepAnchor)
                    cursor.mergeCharFormat(code_format)
                fragments = block.begin()
                while not fragments.atEnd():
                    fragment = fragments.fragment()
                    if fragment.isValid() and fragment.charFormat().isAnchor():
                        color = QTextCharFormat()
                        color.setForeground(QColor("#91caff" if self._dark else "#145fad"))
                        cursor.setPosition(fragment.position())
                        cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.KeepAnchor)
                        cursor.mergeCharFormat(color)
                    fragments += 1
                block = block.next()
        finally:
            cursor.endEditBlock()
            self._applying = False


class MarkdownPresentation(QObject):
    """Presentation helper for editable fields that should never expose Markdown syntax."""

    @Slot(str, result=str)
    def plainText(self, source: str) -> str:
        document = QTextDocument()
        document.setMarkdown(source or "")
        return document.toPlainText()
