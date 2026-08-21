import { describe, expect, it } from "vitest"

import { DEFAULT_MODES } from "../mode.js"

describe("Requirements Engineer default mode", () => {
	const mode = DEFAULT_MODES.find(({ slug }) => slug === "requirements-engineer")

	it("is registered with repository reading and Markdown-only editing", () => {
		expect(mode).toBeDefined()
		expect(mode?.name).toBe("📋 Requirements Engineer")
		expect(mode?.groups).toEqual([
			"read",
			["edit", { fileRegex: "\\.md$", description: "Requirements documents only" }],
		])
	})

	it("covers requirement inputs, analysis, and explicit result categories", () => {
		expect(mode?.roleDefinition).toContain("stakeholder goals, feature descriptions, observed existing behavior")
		expect(mode?.customInstructions).toContain("project context")
		expect(mode?.customInstructions).toContain("Required behavior and constraints")
		expect(mode?.customInstructions).toContain("Edge cases and failure behavior")
		expect(mode?.customInstructions).toContain("Ambiguities, contradictions, and missing information")
		expect(mode?.customInstructions).toContain("Acceptance criteria")
		expect(mode?.customInstructions).toContain("Existing behavior that must remain unchanged")
		expect(mode?.customInstructions).toContain("separate confirmed requirements, assumptions, and open questions")
	})

	it("preserves role boundaries and prohibits invented scope or design", () => {
		expect(mode?.customInstructions).toContain("Requirements Engineer defines what must be true")
		expect(mode?.customInstructions).toContain("Architect owns architecture and design")
		expect(mode?.customInstructions).toContain("Implementation Planner owns implementation sequencing")
		expect(mode?.customInstructions).toContain("Code owns implementation")
		expect(mode?.customInstructions).toContain("Do not invent unsupported functionality")
		expect(mode?.customInstructions).toContain("unrequested implementation prescriptions")
	})
})

describe("Git Committer default mode", () => {
	const mode = DEFAULT_MODES.find(({ slug }) => slug === "git-committer")

	it("is registered with only read and command permissions", () => {
		expect(mode).toBeDefined()
		expect(mode?.name).toBe("📦 Git Committer")
		expect(mode?.groups).toEqual(["read", "command"])
	})

	it("limits the mode to approved commit preparation", () => {
		expect(mode?.customInstructions).toContain("Stage and commit only the approved, completed work")
		expect(mode?.customInstructions).toContain(
			"Do not modify production code, tests, documentation, configuration, or any other project file",
		)
		expect(mode?.customInstructions).toContain("Do not perform additional implementation")
		expect(mode?.customInstructions).toContain(
			"Do not push, merge, rebase, amend, reset, rewrite history, or publish",
		)
	})

	it("allows non-Git workflows to skip the mode", () => {
		expect(mode?.whenToUse).toContain("For non-Git work, report that this mode is inapplicable")
		expect(mode?.customInstructions).toContain("do not treat that as failure of the overall workflow")
	})
})

describe("Verification and Validation Engineer default mode", () => {
	const mode = DEFAULT_MODES.find(({ slug }) => slug === "verification-validation-engineer")
	const prompt = [mode?.roleDefinition, mode?.whenToUse, mode?.customInstructions].join(" ")

	it("is registered with evidence-gathering and report-only edit permissions", () => {
		expect(mode).toBeDefined()
		expect(mode?.name).toBe("✅ Verification and Validation Engineer")
		expect(mode?.groups).toEqual([
			"read",
			["edit", { fileRegex: "\\.md$", description: "Verification evidence and reports only" }],
			"command",
			"mcp",
		])
	})

	it("covers required evidence targets and rejects pass-only reasoning", () => {
		for (const target of [
			"requirements",
			"acceptance criteria",
			"design",
			"interfaces",
			"integration",
			"boundaries",
			"failure modes",
			"regression",
			"intended use",
		]) {
			expect(prompt.toLowerCase()).toContain(target)
		}
		expect(prompt).toContain("Do not assume passing tests or successful execution alone proves correctness")
		expect(prompt).toContain("missing coverage, misleading tests")
		expect(prompt).toContain("unverified assumptions")
	})

	it("distinguishes verification, validation, Code Review, and Code responsibilities", () => {
		expect(prompt).toMatch(/Verification asks whether.*conforms/is)
		expect(prompt).toMatch(/Validation asks whether.*intended purpose/is)
		expect(prompt).toMatch(/Code Review focuses on maintainability/is)
		expect(prompt).toContain("real production behavior and lightweight real infrastructure")
		expect(prompt).toContain("Never weaken, delete, skip, or reinterpret tests or acceptance criteria")
		expect(prompt).toContain("Do not modify implementation code, configuration, or tests")
		expect(prompt).toContain("transition to Code when implementation is authorized")
		expect(prompt).toContain("Production Test Design skill")
	})
})

describe("Code Reviewer default mode", () => {
	const mode = DEFAULT_MODES.find(({ slug }) => slug === "code-reviewer")
	const prompt = [mode?.roleDefinition, mode?.whenToUse, mode?.customInstructions].join(" ")

	it("is registered without general edit authority", () => {
		expect(mode).toBeDefined()
		expect(mode?.name).toBe("🔎 Code Reviewer")
		expect(mode?.groups).toEqual(["read", "command", "mcp"])
		expect(mode?.groups).not.toContain("edit")
	})

	it("covers the required engineering-quality review dimensions", () => {
		for (const dimension of [
			"duplication",
			"responsibility boundaries",
			"coupling",
			"abstraction",
			"control flow",
			"error handling",
			"naming",
			"side effects",
			"dependencies",
			"dead code",
			"architectural drift",
			"over-engineering",
			"under-structured code",
		]) {
			expect(prompt.toLowerCase()).toContain(dimension)
		}
	})

	it("requires impact-based, severity-oriented findings rather than preferences", () => {
		expect(prompt).toContain("concrete maintainability or engineering-quality impact")
		expect(prompt).toContain("Report findings by severity, highest first")
		expect(prompt).toContain("Distinguish material findings from optional or stylistic observations")
		expect(prompt).toContain("Do not recommend refactoring merely because another design is possible")
		expect(prompt).toContain("Do not raise pure stylistic preferences")
	})

	it("separates review from functional V&V, user acceptance, and implementation", () => {
		expect(prompt).toContain("Verification and Validation Engineer owns functional correctness")
		expect(prompt).toContain("directly exposes a concrete defect risk")
		expect(prompt).toContain("board's user Review state is user acceptance")
		expect(prompt).toContain("never silently refactor or fix the code while reviewing")
	})
})
