import { describe, expect, it } from "vitest"

import { getBuiltInSkillContent, getBuiltInSkillNames, getBuiltInSkills } from "../built-in-skills"

describe("built-in skills", () => {
	it("provides metadata and content without filesystem access", () => {
		expect(getBuiltInSkillNames()).toEqual([
			"work-definition",
			"agile-ticket-creation",
			"production-test-design",
			"create-mcp-server",
			"create-mode",
		])

		for (const skill of getBuiltInSkills()) {
			expect(skill).toMatchObject({ source: "built-in", path: `<built-in:${skill.name}>` })
			expect(getBuiltInSkillContent(skill.name)).toMatchObject({
				name: skill.name,
				description: skill.description,
				source: "built-in",
			})
			expect(getBuiltInSkillContent(skill.name)?.instructions.length).toBeGreaterThan(0)
		}
	})

	it("provides a complete Work Definition skill in analysis-oriented modes", () => {
		const metadata = getBuiltInSkills().find(({ name }) => name === "work-definition")
		const content = getBuiltInSkillContent("work-definition")

		expect(metadata).toMatchObject({
			source: "built-in",
			modeSlugs: ["architect", "scrum-master"],
		})
		for (const section of [
			"Motivating Problem or Opportunity",
			"User Intent",
			"Relevant Current State",
			"Desired End State",
			"Included Scope",
			"Excluded Scope",
			"Constraints",
			"Preserved Behavior",
			"Major Work Areas",
			"Dependencies",
			"Risks",
			"Open Questions",
			"Decisions Made",
			"Proposals",
			"Rejected Alternatives",
			"Unresolved Matters",
		]) {
			expect(content?.instructions).toContain(section)
		}
		expect(content?.instructions).toContain("Do not create tickets")
		expect(content?.instructions).toContain("Do not fragment it into tickets")
	})

	it("restricts Agile Ticket Creation to Scrum Master mode", () => {
		expect(getBuiltInSkills().find(({ name }) => name === "agile-ticket-creation")?.modeSlugs).toEqual([
			"scrum-master",
		])
		expect(getBuiltInSkillContent("agile-ticket-creation")?.instructions).toContain(
			"scope, requirements, dependencies, acceptance criteria, and validation expectations",
		)
	})

	it("restricts Production Test Design to Verification and Validation Engineer mode", () => {
		expect(getBuiltInSkills().find(({ name }) => name === "production-test-design")?.modeSlugs).toEqual([
			"verification-validation-engineer",
		])
		expect(getBuiltInSkillContent("production-test-design")?.instructions).toContain(
			"real production code and lightweight real infrastructure",
		)
	})
})
