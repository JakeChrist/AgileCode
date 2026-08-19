import type { BoardScope, BoardScopesEvent } from "@roo-code/types"
import * as vscode from "vscode"

import { discoverVscodeBoardScopes, resolveVscodeBoardScope } from "./resolveVscodeBoardScope"

const selectionFile = ".agilecode/last-board-scope"

/** Resolves automatic and explicit board selection without ever reusing data under another identity. */
export class BoardScopeSelector implements vscode.Disposable {
	private disposables: vscode.Disposable[] = []
	private explicitId: string | undefined
	private currentId: string | undefined
	private started = false

	constructor(
		private readonly select: (scope: BoardScope) => Promise<void>,
		private readonly post: (event: BoardScopesEvent) => void | Promise<void>,
		private readonly log: (message: string) => void,
	) {}

	async start(): Promise<void> {
		if (this.started) return this.refresh()
		this.started = true
		this.explicitId = await this.readPersistedId()
		this.disposables.push(vscode.window.onDidChangeActiveTextEditor(() => void this.refresh()))
		this.disposables.push(vscode.workspace.onDidChangeWorkspaceFolders(() => void this.refresh()))
		await this.refresh()
	}

	async choose(scope: BoardScope): Promise<void> {
		const scopes = await discoverVscodeBoardScopes()
		const valid = scopes.find(({ id }) => id === scope.id)
		if (!valid) {
			this.currentId = undefined
			return this.publish(scopes, undefined, scope.id)
		}
		this.explicitId = valid.id
		await this.persistId(valid.id)
		await this.activate(valid, scopes)
	}

	async refresh(): Promise<void> {
		try {
			const scopes = await discoverVscodeBoardScopes()
			const activeUri = vscode.window.activeTextEditor?.document.uri
			const active = activeUri?.scheme === "file" ? await resolveVscodeBoardScope(activeUri) : undefined
			const selected =
				scopes.find(({ id }) => id === active?.id) ?? scopes.find(({ id }) => id === this.explicitId)
			if (selected) await this.activate(selected, scopes)
			else {
				// Do not leave the previous service associated with a label that is no
				// longer discoverable. If it returns, it must be loaded authoritatively.
				this.currentId = undefined
				await this.publish(scopes, undefined, this.explicitId)
			}
		} catch (error) {
			this.log(`Unable to resolve board scopes: ${String(error)}`)
		}
	}

	dispose(): void {
		for (const disposable of this.disposables) disposable.dispose()
		this.disposables = []
	}

	private async activate(scope: BoardScope, scopes: BoardScope[]): Promise<void> {
		if (scope.id !== this.currentId) {
			this.currentId = scope.id
			await this.select(scope)
		}
		await this.publish(scopes, scope.id)
	}

	private async publish(scopes: BoardScope[], selectedBoardId?: string, unavailableBoardId?: string): Promise<void> {
		await this.post({ type: "board_scopes_changed", scopes, selectedBoardId, unavailableBoardId })
	}

	private storageUri(): vscode.Uri | undefined {
		const root = vscode.workspace.workspaceFolders?.[0]?.uri
		return root && root.scheme === "file" ? vscode.Uri.joinPath(root, selectionFile) : undefined
	}

	private async readPersistedId(): Promise<string | undefined> {
		const uri = this.storageUri()
		if (!uri) return undefined
		try {
			return new TextDecoder().decode(await vscode.workspace.fs.readFile(uri)).trim() || undefined
		} catch {
			return undefined
		}
	}

	private async persistId(id: string): Promise<void> {
		const uri = this.storageUri()
		if (!uri) return
		const root = vscode.workspace.workspaceFolders?.[0]?.uri
		if (!root) return
		await vscode.workspace.fs.createDirectory(vscode.Uri.joinPath(root, ".agilecode"))
		await vscode.workspace.fs.writeFile(uri, new TextEncoder().encode(id))
	}
}
