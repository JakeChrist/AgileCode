"""QML is a presentation adapter. Importing agilecode.core never imports this module."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path


def launch(project=None,global_root=None):
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
    from PySide6.QtQuickControls2 import QQuickStyle
    from .bridge import Bridge
    from .chatstyle import ChatDocumentStyle
    qmlRegisterType(ChatDocumentStyle, "AgileCode.Desktop", 1, 0, "ChatDocumentStyle")
    QQuickStyle.setStyle("Basic")
    application=QApplication.instance() or QApplication(sys.argv[:1])
    application.setOrganizationName("AgileCode");application.setApplicationName("AgileCode")
    if not project:
        project=QFileDialog.getExistingDirectory(None,"Open an original Git project")
        if not project:return 0
    root=Path(project).expanduser().resolve()
    initialize=not (root/".agilecode/agilecode.db").exists()
    if initialize:
        answer=QMessageBox.question(None,"Initialize AgileCode?",f"Create project data under {root}/.agilecode and add .agilecode/ to .gitignore?\n\nYour source files will not be changed.")
        if answer!=QMessageBox.StandardButton.Yes:return 0
    bridge=Bridge(root,global_root,initialize)
    engine=QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend",bridge)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent/"qml/Main.qml")))
    if not engine.rootObjects():
        bridge.close();raise SystemExit("The QML interface failed to load; inspect the Qt diagnostic output.")
    result=application.exec()
    bridge.close()
    return result


def main():
    parser=argparse.ArgumentParser(description="AgileCode QML desktop")
    parser.add_argument("project",nargs="?");parser.add_argument("--global-dir")
    args=parser.parse_args()
    return launch(args.project,args.global_dir)

if __name__=="__main__":main()
