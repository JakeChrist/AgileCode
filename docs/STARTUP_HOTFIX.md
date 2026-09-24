# Desktop startup hotfix

This source package includes fixes identified from the first live PySide6 launches:

- Removed invalid semicolon separators after inline `Text { ... }` values in three QML dialogs. Those separators caused `QQmlApplicationEngine` to reject `Main.qml`.
- Merged two duplicate `onOpened` bindings on the new-ticket dialog. QML permits a property/signal handler to be assigned only once in an object; the duplicate caused the second live startup failure at `Main.qml:551`.
- Made `EngineThread.stop()` safe when QML loading fails before the headless `Application` finishes initializing.
- Added a source-level QML structure regression check that scans every bundled QML file for duplicate bindings and the illegal inline-object separator pattern, so these specific parser failures are caught without needing a Qt runtime.

The Qt runtime itself is still not available in the build environment, so the desktop remains dependent on live validation on a machine with PySide6 installed. Core/runtime tests and the static QML regression checks are separate from that limitation.

Regression coverage is in `tests/test_desktop_startup_sources.py`.
