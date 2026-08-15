import { execFile } from "child_process"
import { createHash } from "crypto"
import { realpath, stat } from "fs/promises"
import * as path from "path"
import { promisify } from "util"

import type { BoardScope } from "@roo-code/types"

const execFileAsync = promisify(execFile)

export type GitRootFinder = (directory: string) => Promise<string | undefined>

export interface ResolveBoardScopeOptions {
	/** File or directory whose board is required (normally the active editor or workspace folder). */
	targetPath: string
	workspaceFolders: readonly string[]
	gitRootFinder?: GitRootFinder
}

async function canonicalPath(candidate: string): Promise<string> {
	const resolved = await realpath(path.resolve(candidate)).catch(() => path.resolve(candidate))
	const normalized = path.normalize(resolved).replaceAll("\\", "/").replace(/\/$/, "")
	return process.platform === "win32" ? normalized.toLowerCase() : normalized
}

function contains(parent: string, child: string): boolean {
	const relative = path.relative(parent, child)
	return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))
}

async function targetDirectory(targetPath: string): Promise<string> {
	const details = await stat(targetPath).catch(() => undefined)
	return details?.isDirectory() ? targetPath : path.dirname(targetPath)
}

export async function findGitRoot(directory: string): Promise<string | undefined> {
	try {
		const { stdout } = await execFileAsync("git", ["rev-parse", "--show-toplevel"], {
			cwd: directory,
			encoding: "utf8",
		})
		return stdout.trim() || undefined
	} catch {
		return undefined
	}
}

export async function createBoardScope(kind: BoardScope["kind"], rootPath: string): Promise<BoardScope> {
	const root = await canonicalPath(rootPath)
	const digest = createHash("sha256").update(`${kind}\0${root}`).digest("hex")
	return { id: `${kind}:${digest}`, kind, rootPath: root }
}

/** Resolves one target without introducing any global "active ticket" state. */
export async function resolveBoardScope(options: ResolveBoardScopeOptions): Promise<BoardScope | undefined> {
	const directory = await canonicalPath(await targetDirectory(options.targetPath))
	const gitRoot = await (options.gitRootFinder ?? findGitRoot)(directory)
	if (gitRoot) return createBoardScope("git", gitRoot)

	const folders = await Promise.all(options.workspaceFolders.map(canonicalPath))
	const workspaceRoot = folders.filter((folder) => contains(folder, directory)).sort((a, b) => b.length - a.length)[0]
	return workspaceRoot ? createBoardScope("workspace", workspaceRoot) : undefined
}
