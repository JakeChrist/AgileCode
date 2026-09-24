---
name: verification-review
description: Verify requirements against observed tool results and independently review complete ticket diffs without changing source.
---
# Evidence-based verification and review
Use only when assigned verification or independent code review.

During verification, check every persisted REQ identifier. Execute the configured tests and inspect relevant source. Record each requirement using record_check with the execution ID returned by the real source tool. A successful test process proves only the behavior covered by those tests; distinguish inspection-based checks from runtime checks.

For a failure, give the implementation member the actual error, affected requirement, reproduction command, and a concrete correction target. Do not fix the implementation yourself. If code changes, earlier verification applies to an old tree and must be repeated.

During review, inspect the complete ticket or integration diff, including added and removed files. Check requirements, interface changes, regressions, unnecessary scope, error paths, and test quality. Return CHANGES_REQUIRED for specific unresolved findings. PASS is not appropriate merely because the programmer reports success.

For an integration candidate, assess both branches' intent. A conflict-free Git merge is not proof that combined behavior is correct.
