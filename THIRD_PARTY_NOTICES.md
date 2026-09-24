# Dependencies and provenance

AgileCode's implementation in this source package was written for this project. No Zoo Code, Roo Code or Cline implementation files, prompt set, frontend bundle, or migration layer are copied into the package. The retained design document cites those projects as behavioral references.

This ZIP does not bundle third-party Python distributions, Qt binaries, Git, Bash, pretrained weights or font files. Install-time dependencies keep their upstream licenses; consult the exact installed versions before distributing binaries. This file is not a replacement for upstream notices or a license determination for a particular distribution.

| Dependency | Purpose | Authoritative project |
| --- | --- | --- |
| CPython / standard library | Runtime, asyncio, SQLite interface | https://www.python.org/ |
| Git | Worktrees, source history, checkpoint objects, bundles | https://git-scm.com/ |
| Bash | Persistent ticket shell | https://www.gnu.org/software/bash/ |
| HTTPX | Compatible model endpoint and HTTP MCP transport | https://www.python-httpx.org/ |
| Pydantic | Validated runtime configuration/contracts | https://docs.pydantic.dev/ |
| regex | Bounded regex searching | https://github.com/mrabarnett/mrab-regex |
| PyYAML | Safe skill metadata parsing | https://pyyaml.org/ |
| jsonschema | MCP tool argument validation | https://python-jsonschema.readthedocs.io/ |
| PySide6 / Qt Quick / Qt Widgets | Optional desktop frontend | https://doc.qt.io/qtforpython-6/licenses.html |
| Pygments | Optional source syntax coloring | https://pygments.org/ |
| pytest / pytest-asyncio | Development tests | https://docs.pytest.org/ and https://pytest-asyncio.readthedocs.io/ |
| ReportLab | Regenerating the PDF manual | https://www.reportlab.com/ |

No overall open-source or commercial distribution license is selected for the user's project by this implementation package. The owner should choose it deliberately before publication. Third-party obligations remain independent of that choice.
