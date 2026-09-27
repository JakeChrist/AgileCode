---
name: production-test-design
description: Design verification that exercises real production behavior at the narrowest adequate boundary without replacing the behavior under test with mocks.
---
# Production Test Design

Use this skill when designing or executing verification and validation for completed or partially completed ticket work.

Start from the ticket requirements and acceptance criteria. For each required behavior, identify the observable evidence that would demonstrate success or failure. Prefer the narrowest test boundary that still executes the real production behavior being validated.

Do not replace the behavior under validation with a mock, fake, stub, duplicated implementation, or hand-authored substitute merely because doing so is easier. A test that substitutes the relevant production behavior cannot establish that the production behavior works. Mocks or other substitutes are acceptable only at genuinely external boundaries that cannot reasonably participate in the test; make that boundary and the resulting limitation explicit.

Cover applicable successful behavior, failure behavior, boundary conditions, preserved existing behavior, integration effects, and regression risks. Reuse the project's established test infrastructure where it provides real evidence. Add or revise tests when coverage is missing, but do not weaken requirements, assertions, or existing meaningful tests to obtain a passing result.

During execution, distinguish evidence produced by tests from evidence produced by source inspection or other tools. A passing command establishes only what that command actually exercised. Record gaps rather than converting assumptions into passes.
