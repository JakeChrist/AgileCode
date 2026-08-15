import { execFile } from "child_process"
import * as fs from "fs/promises"
import * as os from "os"
import * as path from "path"
import { promisify } from "util"

import { resolveBoardScope } from "../board-scope-resolver.js"

const execFileAsync = promisify(execFile)

describe("repository-scoped board resolution", () => {
	let tempDir: string

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "board-scope-test-"))
	})

	afterEach(async () => fs.rm(tempDir, { recursive: true, force: true }))

	async function repository(name: string): Promise<string> {
		const root = path.join(tempDir, name)
		await fs.mkdir(root, { recursive: true })
		await execFileAsync("git", ["init"], { cwd: root })
		return root
	}

	it("gives repositories in a multi-root workspace different identities", async () => {
		const first = await repository("first")
		const second = await repository("second")
		const firstScope = await resolveBoardScope({ targetPath: first, workspaceFolders: [first, second] })
		const secondScope = await resolveBoardScope({ targetPath: second, workspaceFolders: [first, second] })

		expect(firstScope?.kind).toBe("git")
		expect(firstScope?.id).not.toBe(secondScope?.id)
	})

	it("uses one repository-root board for separate monorepository packages", async () => {
		const root = await repository("mono")
		const app = path.join(root, "apps", "web")
		const library = path.join(root, "packages", "shared")
		await fs.mkdir(app, { recursive: true })
		await fs.mkdir(library, { recursive: true })

		const appScope = await resolveBoardScope({ targetPath: app, workspaceFolders: [root] })
		const libraryScope = await resolveBoardScope({ targetPath: library, workspaceFolders: [root] })

		expect(appScope?.id).toBe(libraryScope?.id)
		expect(appScope?.rootPath).toBe(await fs.realpath(root))
	})

	it("resolves an opened repository subdirectory to the repository root", async () => {
		const root = await repository("repo")
		const openedFolder = path.join(root, "nested", "project")
		await fs.mkdir(openedFolder, { recursive: true })

		const scope = await resolveBoardScope({ targetPath: openedFolder, workspaceFolders: [openedFolder] })

		expect(scope?.kind).toBe("git")
		expect(scope?.rootPath).toBe(await fs.realpath(root))
	})

	it("falls back to the containing workspace folder outside Git", async () => {
		const workspace = path.join(tempDir, "plain-workspace")
		const nestedFile = path.join(workspace, "src", "index.ts")
		await fs.mkdir(path.dirname(nestedFile), { recursive: true })
		await fs.writeFile(nestedFile, "")

		const scope = await resolveBoardScope({ targetPath: nestedFile, workspaceFolders: [workspace] })

		expect(scope?.kind).toBe("workspace")
		expect(scope?.rootPath).toBe(await fs.realpath(workspace))
	})
})
