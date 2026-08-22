import type { AgileCodeProjectStore, BoardScope, Ticket } from "@roo-code/types"
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
	it("starts one ordinary task for duplicate activation and confirms the authoritative association", async () => {
		const messages: any[] = []
		const selected = scope("e")
		const readyTicket = {
			formatVersion: 1,
			id: "AC-068",
			statementOfWork: {
				title: "Execute a ticket",
				objective: "Start the task runtime",
				context: "",
				requirements: [],
				constraints: [],
				includedScope: [],
				dependencies: [],
				acceptanceCriteria: [],
				validation: [],
			},
			lifecycle: {
				state: "ready" as const,
				createdAt: "2026-08-22T00:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: [] },
		}
		const selectedState = state(selected)
		selectedState.activeTickets = [readyTicket]
		selectedState.board.columns.ready = [readyTicket.id]
		const startedTicket = {
			...readyTicket,
			lifecycle: { ...readyTicket.lifecycle, state: "in_progress" as const },
			execution: { historyItemIds: ["task-068"] },
		}
		const startExecution = vi.fn(async () => ({
			ok: true as const,
			value: { allowed: true },
			state: { ...selectedState, activeTickets: [startedTicket] },
		}))
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(async () => ({
				state: selectedState,
				recoveryDiagnostics: [],
				dispose: vi.fn(),
				startExecution,
			})) as any,
			async () => true,
			vi.fn(async () => ({
				ok: true as const,
				instruction: "authoritative AC-068 instruction",
				ticket: readyTicket,
				board: selected,
			})),
		)
		await publisher.select(selected)
		const createTask = vi.fn(async () => ({ historyItemId: "task-068" }))
		const request = {
			requestId: "execute-once",
			boardId: selected.id,
			operation: "start_ticket_execution" as const,
			ticketId: "AC-068",
		}

		await Promise.all([
			publisher.handleExecution(request, createTask),
			publisher.handleExecution(request, createTask),
		])

		expect(createTask).toHaveBeenCalledOnce()
		expect(createTask).toHaveBeenCalledWith("authoritative AC-068 instruction", selected.rootPath)
		expect(startExecution).toHaveBeenCalledOnce()
		expect(startExecution).toHaveBeenCalledWith("AC-068", "task-068")
		expect(messages.at(-1)).toMatchObject({
			result: { ok: true, historyItemId: "task-068", ticket: { lifecycle: { state: "in_progress" } } },
		})
	})

	it("leaves a ticket Ready and reports a task-creation failure", async () => {
		const messages: any[] = []
		const selected = scope("d")
		const selectedState = state(selected)
		const startExecution = vi.fn()
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(async () => ({
				state: selectedState,
				recoveryDiagnostics: [],
				dispose: vi.fn(),
				startExecution,
			})) as any,
			async () => true,
			vi.fn(async () => ({ ok: true as const, instruction: "instruction", ticket: {} as any, board: selected })),
		)
		await publisher.select(selected)

		await publisher.handleExecution(
			{
				requestId: "failed-start",
				boardId: selected.id,
				operation: "start_ticket_execution",
				ticketId: "AC-068",
			},
			async () => {
				throw new Error("Provider is unavailable")
			},
		)

		expect(startExecution).not.toHaveBeenCalled()
		expect(selectedState.board.columns.in_progress).toEqual([])
		expect(messages.at(-1)).toMatchObject({
			result: { ok: false, error: { code: "execution_failed", message: "Provider is unavailable" } },
		})
	})

	it("resumes the latest task for a resumable Blocked ticket without creating unrelated history", async () => {
		const messages: any[] = []
		const selected = scope("a")
		const blockedTicket: Ticket = {
			formatVersion: 1,
			id: "AC-077",
			statementOfWork: {
				title: "Resume a ticket",
				objective: "Continue the existing task",
				context: "",
				requirements: [],
				constraints: [],
				includedScope: [],
				dependencies: [],
				acceptanceCriteria: [],
				validation: [],
			},
			lifecycle: {
				state: "blocked" as const,
				createdAt: "2026-08-22T00:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: ["task-old", "task-077"] },
		}
		const selectedState = state(selected)
		selectedState.activeTickets = [blockedTicket]
		selectedState.board.columns.blocked = [blockedTicket.id]
		const resumedTicket = {
			...blockedTicket,
			lifecycle: { ...blockedTicket.lifecycle, state: "in_progress" as const },
		}
		const startExecution = vi.fn()
		const resumeExecution = vi.fn(async () => ({
			ok: true as const,
			value: { allowed: true },
			state: { ...selectedState, activeTickets: [resumedTicket] },
		}))
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(async () => ({
				state: selectedState,
				recoveryDiagnostics: [],
				dispose: vi.fn(),
				startExecution,
				resumeExecution,
			})) as any,
			async () => true,
			vi.fn(async () => ({
				ok: true as const,
				instruction: "authoritative resume instruction",
				ticket: blockedTicket,
				board: selected,
			})),
		)
		await publisher.select(selected)
		const execute = vi.fn(async (_instruction: string, _rootPath: string, historyItemId?: string) => ({
			historyItemId: historyItemId!,
		}))

		await publisher.handleExecution(
			{
				requestId: "resume-once",
				boardId: selected.id,
				operation: "start_ticket_execution",
				ticketId: "AC-077",
			},
			execute,
		)

		expect(execute).toHaveBeenCalledWith("authoritative resume instruction", selected.rootPath, "task-077")
		expect(resumeExecution).toHaveBeenCalledWith("AC-077", "task-077")
		expect(startExecution).not.toHaveBeenCalled()
		expect(messages.at(-1)).toMatchObject({
			result: { ok: true, historyItemId: "task-077", ticket: { lifecycle: { state: "in_progress" } } },
		})
	})

	it("publishes one deterministic, non-persisted improvement for duplicate activation", async () => {
		const messages: any[] = []
		const selected = scope("a")
		const publisher = new BoardStatePublisher((message) => messages.push(message))
		const improve = vi.fn(async () => ({
			title: "Improved ticket",
			objective: "Clear objective",
			context: "",
			requirements: ["One requirement"],
			constraints: [],
			includedScope: [],
			dependencies: [],
			acceptanceCriteria: [],
			validation: [],
		}))
		const request = {
			requestId: "improve-once",
			boardId: selected.id,
			operation: "improve_ticket_draft" as const,
			roughRequest: "Please make this clear",
		}

		await Promise.all([
			publisher.handleImprovement(request, improve),
			publisher.handleImprovement(request, improve),
		])

		expect(improve).toHaveBeenCalledTimes(1)
		expect(messages.filter(({ type }) => type === "board_result")).toHaveLength(2)
		expect(messages.at(-1)).toMatchObject({
			result: { ok: true, operation: "improve_ticket_draft", draft: { title: "Improved ticket" } },
		})
	})

	it("creates a validated ticket once when the same request is activated twice", async () => {
		const messages: any[] = []
		const selected = scope("f")
		const created = {
			formatVersion: 1,
			id: "AC-ABC123",
			statementOfWork: {
				title: "Durable ticket",
				objective: "",
				context: "",
				requirements: [],
				constraints: [],
				includedScope: [],
				dependencies: [],
				acceptanceCriteria: [],
				validation: [],
			},
			lifecycle: {
				state: "backlog",
				createdAt: "2026-08-20T00:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: [] },
		}
		const createFromStatementOfWork = vi.fn(async () => ({ ok: true, value: created }))
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(
				async () =>
					({
						state: state(selected),
						recoveryDiagnostics: [],
						dispose: vi.fn(),
						createFromStatementOfWork,
					}) as any,
			),
		)
		await publisher.select(selected)
		const request = {
			requestId: "one-operation",
			boardId: selected.id,
			operation: "create_ticket" as const,
			ticket: created.statementOfWork,
		}

		await Promise.all([publisher.handleRequest(request), publisher.handleRequest(request)])

		expect(createFromStatementOfWork).toHaveBeenCalledTimes(1)
		expect(messages.filter(({ type }) => type === "board_result")).toHaveLength(2)
		expect(messages.at(-1)).toMatchObject({
			type: "board_result",
			result: { ok: true, ticket: { id: "AC-ABC123", lifecycle: { state: "backlog" } } },
		})
	})

	it("rejects a stale agent mutation without calling persistence", async () => {
		const selected = scope("a")
		const createFromStatementOfWork = vi.fn()
		const publisher = new BoardStatePublisher(
			undefined,
			vi.fn(
				async () =>
					({
						state: state(selected),
						recoveryDiagnostics: [],
						dispose: vi.fn(),
						createFromStatementOfWork,
					}) as any,
			),
		)
		await publisher.select(selected)
		const result = await publisher.executeAgentRequest(
			{
				requestId: "stale",
				boardId: selected.id,
				operation: "create_ticket",
				ticket: {
					title: "Stale",
					objective: "",
					context: "",
					requirements: [],
					constraints: [],
					includedScope: [],
					dependencies: [],
					acceptanceCriteria: [],
					validation: [],
				},
			},
			0,
		)
		expect(result).toMatchObject({ ok: false, error: { code: "conflict", retryable: true } })
		expect(createFromStatementOfWork).not.toHaveBeenCalled()
	})

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

	it("converges two board instances on the authoritative order after a reorder", async () => {
		const sidebar: any[] = []
		const editor: any[] = []
		const selected = scope("c")
		const serviceState = state(selected)
		serviceState.board.columns.backlog = ["AC-001", "AC-002"]
		let changed: ((change: any) => void | Promise<void>) | undefined
		const reorder = vi.fn(async (_column, orderedIds: string[]) => {
			serviceState.board.columns.backlog = [...orderedIds]
			await changed?.({ source: "internal", state: serviceState, diagnostics: [] })
			return { ok: true, value: undefined, state: serviceState }
		})
		const publisher = new BoardStatePublisher(
			(message) => sidebar.push(message),
			vi.fn(async (_scope, options) => {
				changed = options.onDidChange
				return { state: serviceState, recoveryDiagnostics: [], reorder, dispose: vi.fn() } as any
			}),
		)

		await publisher.select(selected)
		publisher.subscribe((message) => editor.push(message))
		await publisher.handleRequest({
			requestId: "reorder-1",
			boardId: selected.id,
			operation: "reorder_tickets",
			state: "backlog",
			orderedIds: ["AC-002", "AC-001"],
			expectedOrder: ["AC-001", "AC-002"],
		})

		const sidebarState = sidebar.filter(({ type }) => type === "board_state_changed").at(-1)
		const editorState = editor.filter(({ type }) => type === "board_state_changed").at(-1)
		expect(editorState).toEqual(sidebarState)
		expect(editorState.snapshot.board.columns.backlog).toEqual(["AC-002", "AC-001"])
		expect(sidebar.at(-1)).toMatchObject({
			type: "board_result",
			result: { operation: "reorder_tickets", ok: true, board: serviceState.board },
		})
	})

	it("routes status-control moves with their destination position and returns the persisted board", async () => {
		const messages: any[] = []
		const selected = scope("d")
		const serviceState = state(selected)
		const movedTicket: Ticket = {
			formatVersion: 1,
			id: "AC-036",
			statementOfWork: {
				title: "Move ticket",
				objective: "Reorganize work",
				context: "Board",
				requirements: ["Persist movement"],
				constraints: [],
				includedScope: ["Board"],
				dependencies: [],
				acceptanceCriteria: ["Moved"],
				validation: ["Test"],
			},
			lifecycle: {
				state: "blocked",
				createdAt: "2026-08-20T00:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
			},
			execution: { historyItemIds: [] },
		}
		serviceState.activeTickets.push(movedTicket)
		serviceState.board.columns.blocked.push(movedTicket.id)
		const moveToPosition = vi.fn(async () => {
			;(movedTicket.lifecycle as { state: string }).state = "ready"
			serviceState.board.columns.blocked = []
			serviceState.board.columns.ready = [movedTicket.id]
			return { ok: true, value: { allowed: true }, state: serviceState }
		})
		const publisher = new BoardStatePublisher(
			(message) => messages.push(message),
			vi.fn(
				async () => ({ state: serviceState, recoveryDiagnostics: [], moveToPosition, dispose: vi.fn() }) as any,
			),
		)
		await publisher.select(selected)

		await publisher.handleRequest({
			requestId: "move-1",
			boardId: selected.id,
			operation: "move_ticket",
			ticketId: movedTicket.id,
			destination: "ready",
			position: 0,
		})

		expect(moveToPosition).toHaveBeenCalledWith(movedTicket.id, "ready", 0, "user", "none")
		expect(messages.at(-1)).toMatchObject({
			type: "board_result",
			result: { operation: "move_ticket", ok: true, ticket: { lifecycle: { state: "ready" } } },
		})
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
