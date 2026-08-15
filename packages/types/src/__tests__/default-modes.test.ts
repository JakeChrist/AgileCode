import { describe, expect, it } from "vitest"

import { modeConfigSchema, DEFAULT_MODES } from "../mode.js"

describe("DEFAULT_MODES", () => {
	it("includes a schema-valid Agile Lead mode with coordination-only permissions", () => {
		const agileLead = DEFAULT_MODES.find((mode) => mode.slug === "agile-lead")

		expect(agileLead).toBeDefined()
		expect(modeConfigSchema.safeParse(agileLead).success).toBe(true)
		expect(agileLead?.groups).toEqual(["read", "mcp"])
		expect(agileLead?.groups).not.toContain("edit")
		expect(agileLead?.groups).not.toContain("command")
	})

	it("keeps Agile Lead separate from Scrum Master ticket authorship and Code implementation", () => {
		const agileLead = DEFAULT_MODES.find((mode) => mode.slug === "agile-lead")
		const prompt = [agileLead?.roleDefinition, agileLead?.whenToUse, agileLead?.customInstructions].join(" ")

		expect(prompt).toMatch(/objectives/i)
		expect(prompt).toMatch(/dependencies/i)
		expect(prompt).toMatch(/priorit/i)
		expect(prompt).toMatch(/unrelated scope/i)
		expect(prompt).toMatch(/Scrum Master.*individual ticket/is)
		expect(prompt).toMatch(/Code.*implement/is)
		expect(prompt).toMatch(/do not implement/i)
	})
})
