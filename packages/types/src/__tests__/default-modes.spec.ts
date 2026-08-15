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
