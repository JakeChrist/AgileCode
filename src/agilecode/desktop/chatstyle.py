"""Presentation-only styling of Qt's already parsed Markdown document."""
import re

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

    @Slot(str, result=str)
    def toolHtml(self, source: str) -> str:
        """Render authored tool prose without interpreting HTML or loading images.

        The temporary document has no resource provider. Replacing its image
        fragments before exposing the generated HTML to QML ensures references
        stay readable without fetching remote or local image resources.
        """
        document = QTextDocument()
        document.setMarkdown(
            source or "",
            QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML,
        )
        replacements = []
        block = document.begin()
        while block.isValid():
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.isValid() and fragment.charFormat().isImageFormat():
                    fmt = fragment.charFormat().toImageFormat()
                    alt = str(fmt.property(QTextFormat.ImageAltText) or "")
                    title = str(fmt.property(QTextFormat.ImageTitle) or "")
                    label = "Image" + (": " + alt if alt else "")
                    label += " (" + fmt.name() + ")"
                    if title:
                        label += " — " + title
                    replacements.append((fragment.position(), fragment.length(), label))
                fragments += 1
            block = block.next()
        # Preserve link destinations as visible text too. Tool output must not
        # conceal data in a hyperlink or provide an application-launch action.
        block = document.begin()
        while block.isValid():
            fragments = block.begin()
            link_start, link_end, link_href, link_text, link_title = None, None, "", "", ""
            while not fragments.atEnd():
                fragment = fragments.fragment()
                fmt = fragment.charFormat()
                href = fmt.anchorHref() if fmt.isAnchor() and not fmt.isImageFormat() else ""
                if link_start is not None and (href != link_href or fragment.position() != link_end):
                    label = link_text if link_text == link_href else link_text + " (" + link_href + ")"
                    if link_title:
                        label += " — " + link_title
                    replacements.append((link_start, link_end - link_start, label))
                    link_start = None
                if href:
                    if link_start is None:
                        link_start, link_href, link_text, link_title = fragment.position(), href, "", fmt.toolTip()
                    link_end = fragment.position() + fragment.length()
                    link_text += fragment.text()
                fragments += 1
            if link_start is not None:
                label = link_text if link_text == link_href else link_text + " (" + link_href + ")"
                if link_title:
                    label += " — " + link_title
                replacements.append((link_start, link_end - link_start, label))
            block = block.next()
        for position, length, label in sorted(replacements, reverse=True):
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.KeepAnchor)
            cursor.insertText(label, QTextCharFormat())
        # Qt's Markdown parser drops the outer destination of a linked image.
        # Retain any target/title absent from the rendered prose as visible text.
        plain = document.toPlainText()
        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.End)
        for target, title in self._tool_reference_targets(source or ""):
            if target and target not in plain:
                label = "Link destination: " + target + (" — " + title if title else "")
            elif title and title not in plain:
                label = "Link title: " + title + " (" + target + ")"
            else:
                continue
            cursor.insertBlock()
            cursor.insertText(label, QTextCharFormat())
            plain += "\n" + label
        return document.toHtml()

    @staticmethod
    def _tool_reference_targets(source: str):
        """Inventory Markdown targets without executing or fetching them.

        This only recovers display metadata that Qt can discard; it is not a
        replacement Markdown renderer. Balanced destinations support URLs with
        parentheses, escaped punctuation and optional quoted titles.
        """
        targets = []
        offset = 0
        while (start := source.find("](", offset)) >= 0:
            position, depth, quote = start + 2, 1, ""
            while position < len(source) and depth:
                character = source[position]
                if character == "\\":
                    position += 2
                    continue
                if quote:
                    if character == quote:
                        quote = ""
                elif character in ("'", '\"') and (position == start + 2 or source[position - 1].isspace()):
                    quote = character
                elif character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                position += 1
            if depth:
                break
            targets.append(source[start + 2:position - 1].strip())
            offset = start + 2  # Also inspect an image nested inside a link.
        targets.extend(match.group(1) for match in re.finditer(r"(?m)^ {0,3}\[[^]\n]+\]:[ \t]*(.+)$", source))
        for raw in targets:
            match = re.match(r"(?:<([^>]+)>|(\S+))(?:\s+(.+))?$", raw)
            if not match:
                continue
            target = match.group(1) or match.group(2)
            title = (match.group(3) or "").strip()
            if len(title) >= 2 and (title[0], title[-1]) in (("'", "'"), ('\"', '\"'), ("(", ")")):
                title = title[1:-1]
            yield target, title
