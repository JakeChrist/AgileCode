import * as path from "path"

import { findGitRoot } from "@roo-code/core"

import { repositoryFinder } from "../resolveVscodeBoardScope"

vi.mock("vscode", () => ({
	window: {},
	workspace: {},
	extensions: {},
}))

vi.mock("@roo-code/core", async (importOriginal) => {
	const original = await importOriginal<typeof import("@roo-code/core")>()
	return { ...original, findGitRoot: vi.fn() }
})

describe("VS Code board repository discovery", () => {
	const mockedFindGitRoot = vi.mocked(findGitRoot)

	afterEach(() => vi.clearAllMocks())

	it("selects the repository containing the target in a multi-root workspace", async () => {
		const first = path.resolve("workspace", "first")
		const second = path.resolve("workspace", "second")
		const finder = repositoryFinder([{ rootUri: { fsPath: first } }, { rootUri: { fsPath: second } }] as never)

		expect(await finder(path.join(second, "packages", "app"))).toBe(second)
		expect(mockedFindGitRoot).not.toHaveBeenCalled()
	})

	it("prefers the closest repository for nested repositories", async () => {
		const outer = path.resolve("workspace", "outer")
		const inner = path.join(outer, "vendor", "inner")
		const finder = repositoryFinder([{ rootUri: { fsPath: outer } }, { rootUri: { fsPath: inner } }] as never)

		expect(await finder(path.join(inner, "src"))).toBe(inner)
	})

	it("falls back to Git CLI while the VS Code Git API is still discovering repositories", async () => {
		const root = path.resolve("workspace", "repository")
		mockedFindGitRoot.mockResolvedValue(root)

		expect(await repositoryFinder([])(path.join(root, "opened-subdirectory"))).toBe(root)
		expect(mockedFindGitRoot).toHaveBeenCalledWith(path.join(root, "opened-subdirectory"))
	})
})
