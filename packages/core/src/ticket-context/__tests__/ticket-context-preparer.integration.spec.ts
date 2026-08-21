import { promises as fs } from "node:fs"
import os from "node:os"
import path from "node:path"

import { afterEach, describe, expect, it } from "vitest"

import { prepareTicketContext } from "../ticket-context-preparer.js"

const roots: string[] = []
async function repository(files: Record<string, string>): Promise<string> {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "ticket-context-"))
	roots.push(root)
	for (const [name, content] of Object.entries(files)) {
		await fs.mkdir(path.dirname(path.join(root, name)), { recursive: true })
		await fs.writeFile(path.join(root, name), content)
	}
	return root
}

afterEach(async () => Promise.all(roots.splice(0).map((root) => fs.rm(root, { recursive: true, force: true }))))

describe("prepareTicketContext", () => {
	it("selects relevant tests and project metadata without changing the repository", async () => {
		const root = await repository({
			"package.json": JSON.stringify({ scripts: { test: "vitest" } }),
			"src/math.ts": "export const add = (a, b) => a + b",
			"src/math.test.ts": "test('add', () => expect(add(1, 2)).toBe(3))",
			"docs/unrelated.md": "release history",
		})
		const before = await fs.readdir(root, { recursive: true })

		const context = await prepareTicketContext(root, "Improve the test suite for math")

		expect(context.status).toBe("inspected")
		expect(context.evidence.map(({ path }) => path)).toEqual(
			expect.arrayContaining(["package.json", "src/math.test.ts"]),
		)
		expect(await fs.readdir(root, { recursive: true })).toEqual(before)
	})

	it("records uncertainty when a repository has no relevant files", async () => {
		const context = await prepareTicketContext(await repository({ "asset.bin": "opaque" }), "Improve tests")
		expect(context.evidence).toEqual([])
		expect(context.unresolvedQuestions).not.toEqual([])
	})

	it("uses partial configuration as evidence without claiming missing behavior", async () => {
		const context = await prepareTicketContext(
			await repository({
				"package.json": JSON.stringify({ scripts: {} }),
				"README.md": "Testing is not configured.",
			}),
			"Improve the test suite",
		)
		expect(context.evidence.map(({ path }) => path)).toEqual(expect.arrayContaining(["package.json", "README.md"]))
	})

	it("reports an unavailable repository as an unresolved question", async () => {
		const context = await prepareTicketContext(path.join(os.tmpdir(), "missing-ticket-context"), "Improve tests")
		expect(context.status).toBe("unavailable")
		expect(context.unresolvedQuestions).not.toEqual([])
	})
})
