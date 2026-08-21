import { promises as fs } from "node:fs"
import os from "node:os"
import path from "node:path"

import { afterEach, describe, expect, it } from "vitest"

import { TicketPreparationPermissionBoundary } from "../preparation-permission-boundary.js"
import { prepareTicketContext } from "../ticket-context-preparer.js"

const roots: string[] = []

async function snapshot(root: string): Promise<Record<string, string>> {
	const result: Record<string, string> = {}
	async function visit(relativeDirectory: string): Promise<void> {
		const entries = await fs.readdir(path.join(root, relativeDirectory), { withFileTypes: true })
		for (const entry of entries) {
			const relative = path.join(relativeDirectory, entry.name)
			if (entry.isDirectory()) await visit(relative)
			else result[relative] = await fs.readFile(path.join(root, relative), "utf8")
		}
	}
	await visit("")
	return result
}

afterEach(async () => Promise.all(roots.splice(0).map((root) => fs.rm(root, { recursive: true, force: true }))))

describe("ticket preparation read-only boundary", () => {
	it("leaves repository and board snapshots unchanged after inspection and rejected actions", async () => {
		const root = await fs.mkdtemp(path.join(os.tmpdir(), "ticket-preparation-boundary-"))
		roots.push(root)
		await fs.mkdir(path.join(root, ".agilecode"))
		await fs.mkdir(path.join(root, "src"))
		await fs.writeFile(path.join(root, ".agilecode", "board.json"), '{"tickets":[]}')
		await fs.writeFile(path.join(root, "src", "search.ts"), "export const search = () => []")
		const before = await snapshot(root)
		const boundary = new TicketPreparationPermissionBoundary()

		const context = await boundary.inspect(() => prepareTicketContext(root, "Improve search"))
		expect(context.status).toBe("inspected")
		for (const reject of [
			() => boundary.editProjectFiles(),
			() => boundary.runSideEffectCommand(),
			() => boundary.startTask(),
			() => boundary.mutateBoard(),
		]) {
			expect(reject).toThrow(/Ticket preparation cannot perform/)
		}

		expect(await snapshot(root)).toEqual(before)
	})
})
