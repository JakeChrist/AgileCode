# Source package verification

AgileCode 0.1.0 — design specification v1.1.

This archive contains the existing functional implementation, revalidated from
an extraction of the source release. No new product requirements or alternate
architecture have been introduced. The bundled design text matches the latest
provided v1.1 Markdown byte for byte.

## Executed checks

| Check | Result |
| --- | --- |
| Domain, workspace and shell/protocol | 66 passed; no failures or skips |
| Workflow, controls, Git recovery and integration | 22 passed; no failures or skips |
| Total automated suite | 88 passed |
| Python source compilation | Passed for application, examples and tests |
| Editable installation | Passed, using installed dependencies |
| Python wheel build | Passed |
| CLI help | Passed |
| Complete HTTP fixture demo | All eight steps, real edit/test/commit/merge and cleanup passed |
| PDF manual | Rebuilt from its Markdown and rendered; 26 pages |

Tests were run in two batches because this environment limits individual command
runtime. The complete successful JUnit records are `revalidation-core.xml` and
`revalidation-workflow.xml`; demo output is `revalidation-demo.txt`.

## Reproduce the checks

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python examples/demo.py
```

The demo uses a deterministic local HTTP model-protocol fixture, **not actual
model inference**. It does use real Git, SQLite, files and Bash.

## Unverified deployment behavior

PySide6 was not available. A new installation attempt failed because package
network access was unavailable. GUI/QML startup, rendering, keyboard and
drag-and-drop behavior therefore remain untested in this environment.

There was no live LM Studio or llama.cpp server/model. No claim is made about
real-model accuracy, inference throughput, or full compatibility with every
server configuration. Large-repository performance, screen readers and
cross-platform desktop packaging remain unqualified.

Read `IMPLEMENTATION_STATUS.md` for the full feature and limitation map. This
record is not a claim that all acceptance scenarios in the design have passed.

## Environment

- Python: 3.13.5
- Platform: Linux x86_64
- httpx: 0.28.1
- pydantic: 2.13.4
- pytest: 9.0.2
- pytest-asyncio: 1.3.0
- regex: 2026.5.9
- PyYAML: 6.0.3
- jsonschema: 4.26.0
- reportlab: 4.4.9
- Pygments: 2.20.0
