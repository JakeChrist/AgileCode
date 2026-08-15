import { DEFAULT_MODES } from "../mode.js"

describe("DEFAULT_MODES", () => {
	it("registers Requirements Engineer with explicit role boundaries", () => {
		const mode = DEFAULT_MODES.find(({ slug }) => slug === "requirements-engineer")

		expect(mode).toBeDefined()
		expect(mode?.whenToUse).toContain("before architecture or implementation planning")
		expect(mode?.customInstructions).toContain("Required behavior and constraints")
		expect(mode?.customInstructions).toContain("Existing behavior that must be preserved")
		expect(mode?.customInstructions).toContain("Do not invent unsupported functionality")
		expect(mode?.customInstructions).toContain("Architect chooses system design and technical architecture")
		expect(mode?.customInstructions).toContain("Implementation Planner turns approved requirements")
		expect(mode?.customInstructions).toContain("Code implements the approved solution")
		expect(mode?.customInstructions).toContain("Do not prescribe architecture, file structure, algorithms")
	})

	it("limits Requirements Engineer edits to requirements documents", () => {
		const mode = DEFAULT_MODES.find(({ slug }) => slug === "requirements-engineer")

		expect(mode?.groups).toEqual([
			"read",
			["edit", { fileRegex: "\\.md$", description: "Requirements documents only" }],
		])
	})
})
