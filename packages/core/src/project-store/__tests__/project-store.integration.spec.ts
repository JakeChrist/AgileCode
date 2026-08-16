import * as fs from "fs/promises"
import * as path from "path"

import { createBoardScope } from "../../board-scope/board-scope-resolver.js"
import { initializeAgileCodeStore } from "../project-store.js"

describe("project-local store initialization", () => {
	let root: string

	beforeEach(async () => {
		const testRoot = path.join(process.cwd(), ".tmp")
		await fs.mkdir(testRoot, { recursive: true })
		root = await fs.mkdtemp(path.join(testRoot, "project-store-test-"))
	})

	afterEach(async () => fs.rm(root, { recursive: true, force: true }))

	async function initialize() {
		return initializeAgileCodeStore(await createBoardScope("workspace", root))
	}

	async function snapshot(): Promise<Map<string, string>> {
		const values = new Map<string, string>()
		async function visit(directory: string): Promise<void> {
			for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
				const absolute = path.join(directory, entry.name)
				const relative = path.relative(root, absolute)
				if (entry.isDirectory()) {
					values.set(`${relative}/`, "directory")
					await visit(absolute)
				} else values.set(relative, await fs.readFile(absolute, "utf8"))
			}
		}
		await visit(root)
		return values
	}

	it("creates a valid empty store and does not create or change .gitignore", async () => {
		const ignore = "node_modules/\n"
		await fs.writeFile(path.join(root, ".gitignore"), ignore)

		const result = await initialize()

		expect(result.created).toBe(true)
		expect(result.store.activeTickets).toEqual([])
		expect(result.store.archivedTickets).toEqual([])
		expect(await fs.readdir(path.join(root, ".agilecode"))).toEqual([
			"archive",
			"board.json",
			"settings.json",
			"store.json",
			"tickets",
		])
		expect(await fs.readFile(path.join(root, ".gitignore"), "utf8")).toBe(ignore)
	})

	it("is idempotent and leaves an existing valid store byte-for-byte unchanged", async () => {
		await initialize()
		const before = await snapshot()

		const result = await initialize()

		expect(result.created).toBe(false)
		expect(await snapshot()).toEqual(before)
	})

	it.each([
		["partial", async () => fs.mkdir(path.join(root, ".agilecode"))],
		[
			"malformed",
			async () => {
				await initialize()
				await fs.writeFile(path.join(root, ".agilecode", "board.json"), "{not json\n")
			},
		],
		[
			"unsupported-version",
			async () => {
				await initialize()
				await fs.writeFile(path.join(root, ".agilecode", "store.json"), '{"formatVersion":2}\n')
			},
		],
	] as const)("reports and preserves a %s store", async (reason, arrange) => {
		await arrange()
		const before = await snapshot()

		const operation = initialize()
		await expect(operation).rejects.toMatchObject({ reason })
		expect(await snapshot()).toEqual(before)
	})

	it("does not create .gitignore when it is absent", async () => {
		await initialize()
		await expect(fs.stat(path.join(root, ".gitignore"))).rejects.toMatchObject({ code: "ENOENT" })
	})
})
