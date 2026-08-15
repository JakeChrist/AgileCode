import { describe, expect, it } from "vitest"

import { getBuiltInSkillContent, getBuiltInSkillNames, getBuiltInSkills } from "../built-in-skills"

describe("built-in skills", () => {
	it("provides metadata and content without filesystem access", () => {
		expect(getBuiltInSkillNames()).toEqual(["create-mcp-server", "create-mode"])

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
})
