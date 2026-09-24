"""Selective QWidget source/diff surfaces; never perform filesystem writes here."""
from __future__ import annotations
import difflib
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QTimer, QRect, QSize
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QTextFormat, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QWidget, QMainWindow, QPlainTextEdit, QTextEdit, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel, QSplitter
from pygments import lex
from pygments.lexers import get_lexer_for_filename, get_lexer_by_name, TextLexer
from pygments.styles import get_style_by_name


from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import Slot
from PySide6.QtGui import QTextCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import QMessageBox, QInputDialog
from .syntax import spans
SYNTAX_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="syntax-view")

class Highlight(QSyntaxHighlighter):
    computed = Signal(int, object)
    def __init__(self,editor,filename,dark,language=None):
        super().__init__(editor.document())
        self.editor,self.filename,self.dark,self.language=editor,filename,dark,language
        self.lines={};self.generation=0
        self.computed.connect(self.accept)
        self.timer=QTimer(editor);self.timer.setSingleShot(True);self.timer.setInterval(100);self.timer.timeout.connect(self.rebuild)
        editor.textChanged.connect(lambda:self.timer.start());self.rebuild()
    def rebuild(self):
        self.generation+=1;generation=self.generation
        future=SYNTAX_POOL.submit(spans,self.editor.toPlainText(),self.filename,self.dark,self.language)
        def done(f):
            try:self.computed.emit(generation,f.result())
            except (RuntimeError,ReferenceError):pass
        future.add_done_callback(done)
    @Slot(int, object)
    def accept(self,generation,raw):
        if generation!=self.generation:return
        self.lines={}
        for line,items in raw.items():
            for start,length,style in items:
                fmt=QTextCharFormat()
                if style["color"]:fmt.setForeground(QColor("#"+style["color"]))
                fmt.setFontWeight(QFont.Weight.Bold if style["bold"] else QFont.Weight.Normal)
                fmt.setFontItalic(bool(style["italic"]))
                self.lines.setdefault(line,[]).append((start,length,fmt))
        self.rehighlight()
    def highlightBlock(self,text):
        for start,length,fmt in self.lines.get(self.currentBlock().blockNumber(),[]):self.setFormat(start,length,fmt)


class Gutter(QWidget):
    def __init__(self,editor):super().__init__(editor);self.editor=editor
    def sizeHint(self):return QSize(self.editor.gutter_width(),0)
    def paintEvent(self,event):self.editor.paint_gutter(event)


class CodePane(QPlainTextEdit):
    def __init__(self,text="",filename="source.py",dark=True,language=None,mapping=None,marks=None):
        super().__init__();self.dark=dark;self.mapping=mapping;self.marks=marks or {}
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont));self.font().setPointSize(11)
        font=self.font();font.setPointSize(11);self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ")*4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet("QPlainTextEdit { background: %s; color: %s; border: 0; selection-background-color: #315568; padding: 6px; }" % ("#101820" if dark else "#ffffff","#d8e2ed" if dark else "#233144"))
        self.gutter=Gutter(self);self.blockCountChanged.connect(self.update_margin);self.updateRequest.connect(self.update_gutter)
        self.setPlainText(text);self.highlighter=Highlight(self,filename,dark,language);self.update_margin()
        self.cursorPositionChanged.connect(self.highlight_lines);self.highlight_lines()
    def gutter_width(self):return 20+self.fontMetrics().horizontalAdvance("9")*max(3,len(str(self.blockCount())))
    def update_margin(self,*_):self.setViewportMargins(self.gutter_width(),0,0,0)
    def update_gutter(self,rect,dy):
        if dy:self.gutter.scroll(0,dy)
        else:self.gutter.update(0,rect.y(),self.gutter.width(),rect.height())
        if rect.contains(self.viewport().rect()):self.update_margin()
    def resizeEvent(self,event):
        super().resizeEvent(event);rect=self.contentsRect();self.gutter.setGeometry(QRect(rect.left(),rect.top(),self.gutter_width(),rect.height()))
    def paint_gutter(self,event):
        painter=QPainter(self.gutter);painter.fillRect(event.rect(),QColor("#16212b" if self.dark else "#f1f4f8"))
        painter.setPen(QColor("#7f92a6"));block=self.firstVisibleBlock();number=block.blockNumber()
        top=round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top());bottom=top+round(self.blockBoundingRect(block).height())
        while block.isValid() and top<=event.rect().bottom():
            if block.isVisible() and bottom>=event.rect().top():
                label=self.mapping[number] if self.mapping and number<len(self.mapping) else number+1
                if label is not None:painter.drawText(0,top,self.gutter.width()-9,self.fontMetrics().height(),Qt.AlignmentFlag.AlignRight,str(label))
            block=block.next();top=bottom;bottom=top+round(self.blockBoundingRect(block).height());number+=1
    def highlight_lines(self):
        selections=[]
        for number,kind in self.marks.items():
            block=self.document().findBlockByNumber(number)
            if not block.isValid():continue
            sel=QTextEdit.ExtraSelection();sel.cursor=self.textCursor();sel.cursor.setPosition(block.position());sel.cursor.clearSelection()
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection,True)
            color={"add":"#183e32" if self.dark else "#dff2e6","remove":"#492b32" if self.dark else "#f9e3e5"}[kind]
            sel.format.setBackground(QColor(color));selections.append(sel)
        self.setExtraSelections(selections)


class CodeWindow(QMainWindow):
    save_requested=Signal(str,str)
    def __init__(self,title,text,*,dark=True,editable=False,file_hash=None,language=None):
        super().__init__();self.setWindowTitle("AgileCode — "+title);self.resize(1100,800);self.hash=file_hash;self.file_title=title
        self.setStyleSheet("QWidget { background: %s; color: %s; } QPushButton, QLineEdit { padding: 8px; border: 1px solid #526779; border-radius: 5px; }" % ("#16212b" if dark else "#f1f4f8","#e0e8f0" if dark else "#233144"))
        main=QWidget();layout=QVBoxLayout(main);bar=QHBoxLayout();layout.addLayout(bar)
        bar.addWidget(QLabel(title));bar.addStretch();search=QLineEdit();search.setPlaceholderText("Find in file…");bar.addWidget(search)
        self.pane=CodePane(text,title,dark,language);self.pane.setReadOnly(not editable);layout.addWidget(self.pane)
        search.returnPressed.connect(lambda:self.pane.find(search.text()) or (self.pane.moveCursor(QTextCursor.MoveOperation.Start),self.pane.find(search.text())))
        self.save=QPushButton("Save guarded edit" if editable else "Read only");self.save.setEnabled(editable);bar.addWidget(self.save)
        self.save.clicked.connect(lambda:self.save_requested.emit(self.pane.toPlainText(),self.hash or ""))
        self.pane.document().setModified(False)
        QShortcut(QKeySequence.StandardKey.Find,self,activated=search.setFocus)
        QShortcut(QKeySequence("Ctrl++"),self,activated=lambda:self.pane.zoomIn(1))
        QShortcut(QKeySequence("Ctrl+-"),self,activated=lambda:self.pane.zoomOut(1))
        QShortcut(QKeySequence("Ctrl+L"),self,activated=self.go_to_line)
        self.notice=QLabel("Manual edits invalidate implementation verification/review; resume the process to check them." if editable else "Source view. No files can be modified in this window.")
        layout.addWidget(self.notice);self.setCentralWidget(main)
    def saved(self,result):
        if result.get("path")==self.file_title:
            self.hash=result.get("hash",self.hash);self.pane.document().setModified(False);self.notice.setText("Saved through the headless engine. Resume implementation and verification.")

    def dirty(self):return not self.pane.isReadOnly() and self.pane.document().isModified()
    def go_to_line(self):
        line,ok=QInputDialog.getInt(self,"Go to line","Line number",1,1,self.pane.blockCount())
        if ok:
            cursor=self.pane.textCursor();cursor.setPosition(self.pane.document().findBlockByNumber(line-1).position());self.pane.setTextCursor(cursor);self.pane.centerCursor()
    def closeEvent(self,event):
        if self.dirty():
            answer=QMessageBox.question(self,"Unsaved source buffer","Discard these unsaved edits?",QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if answer!=QMessageBox.StandardButton.Discard:event.ignore();return
            self.pane.document().setModified(False)
        super().closeEvent(event)


class DiffWindow(QMainWindow):
    def __init__(self,document,*,dark=True):
        super().__init__();path=document["path"];self.setWindowTitle("AgileCode — Compare "+path);self.resize(1400,820)
        left=[];right=[];lm=[];rm=[];lmarks={};rmarks={}
        for row in document["rows"]:
            index=len(left);left.append(row["before"]);right.append(row["after"]);lm.append(row["old"]);rm.append(row["new"])
            if row["kind"] not in {"equal","fold"}:
                if row["old"] is not None:lmarks[index]="remove"
                if row["new"] is not None:rmarks[index]="add"
        splitter=QSplitter(Qt.Orientation.Horizontal)
        def panel(title,text,mapping,marks):
            panel=QWidget();layout=QVBoxLayout(panel);layout.setContentsMargins(5,5,5,5);layout.addWidget(QLabel(title))
            pane=CodePane("\n".join(text),path,dark,mapping=mapping,marks=marks);pane.setReadOnly(True);layout.addWidget(pane);splitter.addWidget(panel);return pane
        a=panel("BASE / before",left,lm,lmarks);b=panel("TICKET / after",right,rm,rmarks)
        a.verticalScrollBar().valueChanged.connect(b.verticalScrollBar().setValue);b.verticalScrollBar().valueChanged.connect(a.verticalScrollBar().setValue)
        self.setCentralWidget(splitter)
