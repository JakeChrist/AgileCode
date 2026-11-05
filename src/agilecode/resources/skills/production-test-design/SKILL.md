---
name: production-test-design
description: Design verification that exercises real production behavior at the narrowest adequate boundary without replacing the behavior under test with mocks.
---
# Production Test Design

Use this skill when designing or executing verification and validation for completed or partially completed ticket work.

Start from the original ticket, its justified requirements, and acceptance criteria. Identify observable evidence for each relevant required behavior. Scale test depth to the actual change and credible regression risks; do not invent new acceptance obligations. Prefer the narrowest test boundary that still exercises the real production behavior being validated.

Do not replace the behavior under validation with a mock, fake, stub, duplicated implementation, or hand-authored substitute merely because doing so is easier. A test that substitutes the relevant production behavior cannot establish that the production behavior works. Mocks or other substitutes are acceptable only at genuinely external boundaries that cannot reasonably participate in the test; make that boundary and the resulting limitation explicit.

Consider successful behavior, failure behavior, boundary conditions, existing behavior, integration effects, and regression risks, but cover only the cases warranted by this ticket and its actual changes. Reuse established test infrastructure where it provides real evidence. Do not demand exhaustive testing merely to exercise every category. Do not weaken requirements, assertions, or existing meaningful tests to obtain a passing result.

During execution, distinguish evidence produced by tests from source inspection or other tools. A passing command establishes only what it actually exercised. Record material gaps rather than converting assumptions into passes. Request corrections for demonstrated failures or meaningful unsupported obligations, not optional additional testing.
