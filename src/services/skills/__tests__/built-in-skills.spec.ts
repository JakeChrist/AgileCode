import { describe, expect, it } from "vitest"

import { getBuiltInSkillContent, getBuiltInSkillNames, getBuiltInSkills } from "../built-in-skills"
import { agileTicketCreationFixtures } from "./fixtures/agile-ticket-creation"

describe("built-in skills", () => {
	it("provides metadata and content without filesystem access", () => {
		expect(getBuiltInSkillNames()).toEqual([
			"work-definition",
			"agile-ticket-creation",
			"agile-ticket-implementation",
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

	it("defines the board-aware Agile Ticket Implementation workflow", () => {
		const metadata = getBuiltInSkills().find(({ name }) => name === "agile-ticket-implementation")
		const instructions = getBuiltInSkillContent("agile-ticket-implementation")?.instructions

		expect(metadata).toMatchObject({ source: "built-in", modeSlugs: ["orchestrator"] })
		for (const stage of [
			"Requirements Engineer",
			"Architect",
			"Implementation Planner",
			"Code",
			"Verification and Validation Engineer",
			"Production Test Design",
			"Code Reviewer",
			"Git Committer",
		]) {
			expect(instructions).toContain(stage)
		}
		expect(instructions).toContain("Kanban board is the sole authority")
		expect(instructions).toContain("Do not move, rename, copy, or create ticket files")
		expect(instructions).toContain("If V&V fails")
		expect(instructions).toContain("substantive finding")
		expect(instructions).toContain("repeat the complete **V&V with Production Test Design** stage")
		expect(instructions).toContain("not the board's **Review** state")
		expect(instructions).toContain("not a Git repository, skip Git Committer")
		expect(instructions).toContain("Never declare the ticket accepted or Done")
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

	it("makes Agile Ticket Creation available in its registered ticket-preparation mode", () => {
		expect(getBuiltInSkills().find(({ name }) => name === "agile-ticket-creation")?.modeSlugs).toEqual([
			"scrum-master",
		])
		const instructions = getBuiltInSkillContent("agile-ticket-creation")?.instructions
		for (const section of [
			"Determine the real outcome",
			"Decompose before drafting",
			"Included Scope",
			"Preserved Behavior",
			"Constraints",
			"Out of Scope",
			"Requirements",
			"Dependencies",
			"Acceptance Criteria",
			"Validation",
		]) {
			expect(instructions).toContain(section)
		}
		expect(instructions).toContain("does not authorize or begin execution")
	})

	it.each(agileTicketCreationFixtures)("covers the $name ticket-quality fixture", ({ requiredGuidance }) => {
		const instructions = getBuiltInSkillContent("agile-ticket-creation")?.instructions
		for (const guidance of requiredGuidance) {
			expect(instructions).toContain(guidance)
		}
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
