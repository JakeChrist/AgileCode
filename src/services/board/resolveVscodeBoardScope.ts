import { findGitRoot, resolveBoardScope, type GitRootFinder } from "@roo-code/core"
import type { BoardScope } from "@roo-code/types"
import * as path from "path"
import * as vscode from "vscode"

interface GitRepository {
	rootUri: vscode.Uri
}

interface GitApi {
	repositories: GitRepository[]
}

interface GitExtensionExports {
	getAPI(version: 1): GitApi
}

export function repositoryFinder(repositories: GitRepository[]): GitRootFinder {
	return async (directory) => {
		const matches = repositories
			.map((repository) => repository.rootUri.fsPath)
			.filter((root) => {
				const relative = path.relative(root, directory)
				return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))
			})
			.sort((a, b) => b.length - a.length)
		// The built-in Git extension discovers repositories asynchronously and may not
		// expose a repository yet when a board is first requested. Retain CLI discovery
		// so an opened repository subdirectory never temporarily becomes a workspace board.
		return matches[0] ?? findGitRoot(directory)
	}
}

/** Resolve a board using VS Code's Git repositories, with workspace-folder fallback. */
export async function resolveVscodeBoardScope(resource?: vscode.Uri): Promise<BoardScope | undefined> {
	const target =
		resource ?? vscode.window.activeTextEditor?.document.uri ?? vscode.workspace.workspaceFolders?.[0]?.uri
	if (!target || target.scheme !== "file") return undefined

	const extension = vscode.extensions.getExtension<GitExtensionExports>("vscode.git")
	if (extension && !extension.isActive) await extension.activate()
	const repositories = extension?.exports.getAPI(1).repositories ?? []

	return resolveBoardScope({
		targetPath: target.fsPath,
		workspaceFolders: vscode.workspace.workspaceFolders?.map((folder) => folder.uri.fsPath) ?? [],
		gitRootFinder: repositoryFinder(repositories),
	})
}
