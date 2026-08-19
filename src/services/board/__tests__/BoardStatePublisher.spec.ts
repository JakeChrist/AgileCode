import type { AgileCodeProjectStore, BoardScope } from "@roo-code/types"
import { describe, expect, it, vi } from "vitest"

import { BoardStatePublisher } from "../BoardStatePublisher"

const scope = (suffix: string): BoardScope => ({
	id: `git:${suffix.repeat(64)}`,
	kind: "git",
	rootPath: `/repo/${suffix}`,
})
const state = (selected: BoardScope): AgileCodeProjectStore => ({
	manifest: { formatVersion: 1, scope: selected },
	board: {
		formatVersion: 1,
		columns: { backlog: [], ready: [], in_progress: [], blocked: [], review: [], done: [] },
		archiveOrder: [],
	},
	settings: {
		formatVersion: 1,
		automaticArchival: { enabled: false, retentionDays: 30 },
		repositorySelection: { preferredScopeId: null },
		showArchived: false,
		suppressDragToExecuteWarning: false,
		workflowPreferences: {},
	},
	activeTickets: [],
	archivedTickets: [],
})

describe("BoardStatePublisher", () => {
	it("keeps two concurrently visible consumers synchronized from one service", async () => {
		const sidebar: any[] = []
		const editor: any[] = []
		let changed: ((change: any) => void | Promise<void>) | undefined
		const selected = scope("e")
		const serviceState = state(selected)
		const publisher = new BoardStatePublisher(
			(message) => sidebar.push(message),
			vi.fn(async (_scope, options) => {
				changed = options.onDidChange
				return { state: serviceState, recoveryDiagnostics: [], dispose: vi.fn() } as any
			}),
		)

		await publisher.select(selected)
		const unsubscribe = publisher.subscribe((message) => editor.push(message))
		await changed?.({ source: "internal", state: serviceState, diagnostics: [] })

		expect(editor[0]).toMatchObject({ boardId: selected.id, status: "ready" })
		expect(editor.at(-1)).toEqual(sidebar.at(-1))
		expect(sidebar.at(-1).revision).toBe(2)
		unsubscribe()
	})

	it("publishes loading, initial state, and service updates for the selected repository", async () => {
		const messages: unknown[] = []
		let changed: ((change: any) => void | Promise<void>) | undefined
		const selected = scope("a")
		const serviceState = state(selected)
		const service = {
			state: serviceState,
			recoveryDiagnostics: [{ record: "tickets/bad.json", problem: "invalid JSON", kind: "malformed" }],
			dispose: vi.fn(),
		}
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(async (_scope, options) => {
				changed = options.onDidChange
				return service as any
			}),
		)

		await publisher.select(selected)
		await changed?.({ source: "internal", state: serviceState, diagnostics: [] })

		expect(messages.map((message: any) => message.status)).toEqual(["loading", "ready", "ready"])
		expect((messages[1] as any).snapshot.diagnostics).toHaveLength(1)
		expect((messages[2] as any).boardId).toBe(selected.id)
	})

	it("does not publish completion or failures from a superseded repository", async () => {
		const messages: any[] = []
		const first = scope("a")
		const second = scope("b")
		let finishFirst!: (service: any) => void
		const firstService = new Promise((resolve) => (finishFirst = resolve))
		const factory = vi.fn((selected: BoardScope) => {
			if (selected.id === first.id) return firstService as any
			return Promise.resolve({ state: state(second), recoveryDiagnostics: [], dispose: vi.fn() } as any)
		})
		const publisher = new BoardStatePublisher((message) => messages.push(message), factory)

		const selectingFirst = publisher.select(first)
		await publisher.select(second)
		finishFirst({ state: state(first), recoveryDiagnostics: [], dispose: vi.fn() })
		await selectingFirst

		expect(messages.filter((message) => message.status === "ready").map((message) => message.boardId)).toEqual([
			second.id,
		])
	})

	it("turns malformed-store load errors into an identity-bound typed failure", async () => {
		const messages: any[] = []
		const selected = scope("c")
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(async () => {
				throw new Error("board.json is malformed")
			}),
			vi.fn(async () => true),
		)

		await publisher.select(selected)

		expect(messages[1]).toMatchObject({
			boardId: selected.id,
			status: "error",
			error: { operation: "load_board", code: "persistence_failed" },
		})
		expect(messages[1]).not.toHaveProperty("snapshot")
	})

	it("distinguishes an absent store from a fatal load failure", async () => {
		const messages: any[] = []
		const selected = scope("d")
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(async () => {
				throw new Error("store.json is missing")
			}),
			vi.fn(async () => false),
		)

		await publisher.select(selected)

		expect(messages.map(({ status }) => status)).toEqual(["loading", "uninitialized"])
		expect(messages[1]).not.toHaveProperty("error")
	})
})
