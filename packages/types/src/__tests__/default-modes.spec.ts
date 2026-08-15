import { describe, expect, it } from "vitest"

import { DEFAULT_MODES } from "../mode.js"

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
