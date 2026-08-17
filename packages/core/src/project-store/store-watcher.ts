import * as fsSync from "fs"
import * as path from "path"

import type { AgileCodeProjectStore, Ticket } from "@roo-code/types"

import { reconcileBoardOrdering } from "./board-persistence.js"
import { loadAgileCodeStore, type AgileCodeStoreDiagnostic, type AgileCodeStoreLoadResult } from "./project-store.js"

const DEFAULT_DEBOUNCE_MS = 100

export interface AgileCodeStoreChange extends AgileCodeStoreLoadResult {
	/** Paths reported by the watcher, relative to `.agilecode/`. */
	changedRecords: string[]
}

export interface AgileCodeStoreWatcherOptions {
	debounceMs?: number
	onDidChange(change: AgileCodeStoreChange): void | Promise<void>
	onDidError?(error: unknown): void | Promise<void>
}

function diagnosticRecords(diagnostics: AgileCodeStoreDiagnostic[], directory: "tickets" | "archive"): Set<string> {
	return new Set(
		diagnostics
			.filter(({ kind, record }) => kind !== "reconciled" && record.startsWith(`${directory}/`))
			.map(({ record }) => path.basename(record, ".json")),
	)
}

function retainInvalidTickets(current: Ticket[], previous: Ticket[], invalidIds: Set<string>): Ticket[] {
	const loadedIds = new Set(current.map(({ id }) => id))
	return current.concat(previous.filter(({ id }) => invalidIds.has(id) && !loadedIds.has(id)))
}

/**
 * External malformed writes are isolated in the same way as startup loading, but a
 * record that was already usable remains visible until it becomes valid or is
 * deleted. A malformed board or settings file likewise retains its last valid
 * value instead of replacing the active UI with recovery defaults.
 */
export function retainLastValidStore(
	previous: AgileCodeProjectStore,
	loaded: AgileCodeStoreLoadResult,
): AgileCodeStoreLoadResult {
	const invalidActive = diagnosticRecords(loaded.diagnostics, "tickets")
	const invalidArchived = diagnosticRecords(loaded.diagnostics, "archive")
	const activeTickets = retainInvalidTickets(loaded.store.activeTickets, previous.activeTickets, invalidActive)
	const archivedTickets = retainInvalidTickets(
		loaded.store.archivedTickets,
		previous.archivedTickets,
		invalidArchived,
	)
	const invalidBoard = loaded.diagnostics.some(({ kind, record }) => kind !== "reconciled" && record === "board.json")
	const invalidSettings = loaded.diagnostics.some(
		({ kind, record }) => kind !== "reconciled" && record === "settings.json",
	)
	const board = reconcileBoardOrdering(
		invalidBoard ? previous.board : loaded.store.board,
		activeTickets,
		archivedTickets,
	)

	return {
		diagnostics: loaded.diagnostics.concat(
			board.issues.map((issue) => ({
				record: "board.json",
				problem: `${issue.kind} for ${issue.id} at ${issue.location}`,
				kind: "reconciled" as const,
			})),
		),
		store: {
			...loaded.store,
			board: board.board,
			settings: invalidSettings ? previous.settings : loaded.store.settings,
			activeTickets,
			archivedTickets,
		},
	}
}

/** Watches a single repository-local store and emits validated, debounced snapshots. */
export class AgileCodeStoreWatcher {
	private readonly storePath: string
	private readonly watchers: fsSync.FSWatcher[] = []
	private readonly pendingRecords = new Set<string>()
	private timer: ReturnType<typeof setTimeout> | undefined
	private disposed = false
	private reloading = false
	private reloadAgain = false

	private constructor(
		private readonly rootPath: string,
		private current: AgileCodeProjectStore,
		private readonly options: AgileCodeStoreWatcherOptions,
	) {
		this.storePath = path.join(rootPath, ".agilecode")
	}

	static async create(rootPath: string, options: AgileCodeStoreWatcherOptions): Promise<AgileCodeStoreWatcher> {
		const initial = await loadAgileCodeStore(rootPath)
		const watcher = new AgileCodeStoreWatcher(rootPath, initial.store, options)
		watcher.startWatching()
		return watcher
	}

	get snapshot(): AgileCodeProjectStore {
		return this.current
	}

	private startWatching(): void {
		this.closeWatchers()
		for (const relative of ["", "tickets", "archive"]) {
			try {
				const directory = path.join(this.storePath, relative)
				const watcher = fsSync.watch(directory, (_event, filename) => {
					const record = filename ? path.join(relative, filename.toString()) : relative || "."
					this.scheduleReload(record)
				})
				watcher.on("error", (error) => void this.reportError(error))
				this.watchers.push(watcher)
			} catch (error) {
				void this.reportError(error)
			}
		}
	}

	private scheduleReload(record: string): void {
		if (this.disposed) return
		this.pendingRecords.add(record)
		if (this.timer) clearTimeout(this.timer)
		this.timer = setTimeout(() => void this.reload(), this.options.debounceMs ?? DEFAULT_DEBOUNCE_MS)
	}

	private async reload(): Promise<void> {
		this.timer = undefined
		if (this.disposed) return
		if (this.reloading) {
			this.reloadAgain = true
			return
		}
		this.reloading = true
		const changedRecords = [...this.pendingRecords].sort()
		this.pendingRecords.clear()
		try {
			const next = retainLastValidStore(this.current, await loadAgileCodeStore(this.rootPath))
			if (this.disposed) return
			this.current = next.store
			this.startWatching()
			await this.options.onDidChange({ ...next, changedRecords })
		} catch (error) {
			await this.reportError(error)
		} finally {
			this.reloading = false
			if (this.reloadAgain || this.pendingRecords.size > 0) {
				this.reloadAgain = false
				this.scheduleReload(".")
			}
		}
	}

	private async reportError(error: unknown): Promise<void> {
		if (!this.disposed) await this.options.onDidError?.(error)
	}

	private closeWatchers(): void {
		for (const watcher of this.watchers.splice(0)) watcher.close()
	}

	dispose(): void {
		this.disposed = true
		if (this.timer) clearTimeout(this.timer)
		this.timer = undefined
		this.pendingRecords.clear()
		this.closeWatchers()
	}
}

export const watchAgileCodeStore = (rootPath: string, options: AgileCodeStoreWatcherOptions) =>
	AgileCodeStoreWatcher.create(rootPath, options)
