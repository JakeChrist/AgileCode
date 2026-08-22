import { describe, expect, it } from "vitest"

import { getBuiltInSkillContent, getBuiltInSkillNames, getBuiltInSkills } from "../built-in-skills"
import { agileTicketCreationFixtures } from "./fixtures/agile-ticket-creation"
import { doubtDrivenDevelopmentFixtures } from "./fixtures/doubt-driven-development"
import { productionTestDesignFixtures } from "./fixtures/production-test-design"
import { rootCauseAnalysisFixtures } from "./fixtures/root-cause-analysis"

describe("built-in skills", () => {
	it("provides metadata and content without filesystem access", () => {
		expect(getBuiltInSkillNames()).toEqual([
			"work-definition",
			"agile-ticket-creation",
			"agile-ticket-implementation",
			"production-test-design",
			"doubt-driven-development",
			"root-cause-analysis",
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

	it("does not let agile skills expand the invoking mode's authority", () => {
		for (const name of [
			"work-definition",
			"agile-ticket-creation",
			"agile-ticket-implementation",
			"production-test-design",
			"doubt-driven-development",
			"root-cause-analysis",
		]) {
			const instructions = getBuiltInSkillContent(name)?.instructions

			expect(instructions).toContain("This skill does not expand the active mode's authority")
			expect(instructions).toContain("transition, delegate, or request the appropriate mode")
			expect(instructions).toContain("outside the active ticket as follow-up work")
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

	it("routes cross-role and out-of-scope discoveries instead of absorbing them", () => {
		const instructions = getBuiltInSkillContent("agile-ticket-implementation")?.instructions

		expect(instructions).toContain("Delegate each stage to its named mode")
		expect(instructions).toContain("return the actionable findings to **Code**")
		expect(instructions).toContain("Route discoveries that alter requirements")
		expect(instructions).toContain("back to **Requirements Engineer**")
		expect(instructions).toContain("architectural decisions back to **Architect**")
		expect(instructions).toContain("sequencing impacts back to **Implementation Planner**")
		expect(instructions).toContain("do not silently expand or reinterpret")
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

	it("supports conversational SOW generation, refinement, and an explicit persistence handoff", () => {
		const instructions = getBuiltInSkillContent("work-definition")?.instructions

		for (const conversationalContext of [
			"agreed decisions",
			"rejected alternatives",
			"confirmed constraints",
			"included and excluded scope",
			"desired outcomes",
			"unresolved questions",
		]) {
			expect(instructions).toContain(conversationalContext)
		}
		expect(instructions).toContain("refine, narrow, or expand")
		expect(instructions).toContain("A refinement is not permission")
		expect(instructions).toContain("turn this into tickets and add them to Backlog")
		expect(instructions).toContain("request a mode transition")
		expect(instructions).toContain("decomposition tool")
		expect(instructions).toContain("Do not require or propose a Convert Discussion button")
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

	it("makes Production Test Design available in approved testing modes", () => {
		expect(getBuiltInSkills().find(({ name }) => name === "production-test-design")?.modeSlugs).toEqual([
			"code",
			"verification-validation-engineer",
		])
		const instructions = getBuiltInSkillContent("production-test-design")?.instructions
		for (const principle of [
			"required behavior outward",
			"Trace the actual production path",
			"narrowest adequate boundary",
			"must not replace the behavior being tested merely for convenience",
			"Substitution is reserved for a genuinely external boundary",
			"observable outputs, state changes, persisted data, messages, requests, rendered UI",
		]) {
			expect(instructions).toContain(principle)
		}
	})

	it.each(productionTestDesignFixtures)("covers the $name evidence-design fixture", ({ requiredGuidance }) => {
		const instructions = getBuiltInSkillContent("production-test-design")?.instructions
		for (const guidance of requiredGuidance) {
			expect(instructions).toContain(guidance)
		}
	})

	it("makes Doubt-Driven Development available in approved engineering modes", () => {
		expect(getBuiltInSkills().find(({ name }) => name === "doubt-driven-development")?.modeSlugs).toEqual([
			"requirements-engineer",
			"architect",
			"implementation-planner",
			"code",
			"debug",
			"verification-validation-engineer",
			"code-reviewer",
		])

		const instructions = getBuiltInSkillContent("doubt-driven-development")?.instructions
		for (const category of [
			"Requirement",
			"Verified fact",
			"Inference",
			"Unverified assumption",
			"Contradiction",
			"Unknown",
		]) {
			expect(instructions).toContain(`**${category}**`)
		}
		expect(instructions).toContain("Apply the materiality test")
		expect(instructions).toContain("could reasonably change the result, scope, acceptance criteria")
		expect(instructions).toContain("Do not inventory or re-check routine, reversible, low-risk decisions")
		expect(instructions).toContain("Never silently promote")
		expect(instructions).toContain("smallest practical resolution")
	})

	it.each(doubtDrivenDevelopmentFixtures)(
		"covers the $name doubt-resolution fixture",
		({ hiddenAssumption, requiredGuidance }) => {
			const instructions = getBuiltInSkillContent("doubt-driven-development")?.instructions
			expect(instructions).toContain(hiddenAssumption)
			for (const guidance of requiredGuidance) {
				expect(instructions).toContain(guidance)
			}
		},
	)

	it("makes Root Cause Analysis discoverable in approved diagnostic modes", () => {
		expect(getBuiltInSkills().find(({ name }) => name === "root-cause-analysis")?.modeSlugs).toEqual([
			"debug",
			"architect",
			"verification-validation-engineer",
		])
		expect(getBuiltInSkills().some(({ name }) => name === "root-cause-analysis")).toBe(true)
	})

	it.each(rootCauseAnalysisFixtures)("covers the $name RCA fixture", ({ requiredGuidance }) => {
		const instructions = getBuiltInSkillContent("root-cause-analysis")?.instructions
		for (const guidance of requiredGuidance) {
			expect(instructions).toContain(guidance)
		}
	})
})
