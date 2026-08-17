import { act, render, screen } from "@testing-library/react"

import type { BoardScope, BoardStateEvent } from "@roo-code/types"

import { BoardStateContextProvider, boardReducer, initialBoardState, useBoardState } from "../BoardStateContext"

const id = (letter: string) => `git:${letter.repeat(64)}`
const scope = (letter: string): BoardScope => ({ id: id(letter), kind: "git", rootPath: `/repo-${letter}` })
const statementOfWork = {
	title: "A ticket",
	objective: "Test state",
	context: "Webview",
	requirements: ["Keep drafts"],
	constraints: ["Stay local"],
	includedScope: ["Board"],
	dependencies: [],
	acceptanceCriteria: ["Pass"],
	validation: ["Test"],
}
const readyEvent = (selected: BoardScope, revision = 1): BoardStateEvent => ({
	type: "board_state_changed",
	boardId: selected.id,
	scope: selected,
	revision,
	status: "ready",
	snapshot: {
		scope: selected,
		board: {
			formatVersion: 1,
			columns: { backlog: ["AC-013"], ready: [], in_progress: [], blocked: [], review: [], done: [] },
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
		activeTickets: [
			{
				formatVersion: 1,
				id: "AC-013",
				statementOfWork,
				lifecycle: {
					state: "backlog",
					createdAt: "2026-08-14T00:00:00.000Z",
					reviewComments: [],
					blockedReasons: [],
					failedAttempts: [],
				},
				execution: { historyItemIds: [] },
			},
		],
		archivedTickets: [],
		diagnostics: [],
	},
})

describe("boardReducer", () => {
	it("keys authoritative updates by board and leaves the selected board visible", () => {
		const a = scope("a")
		const b = scope("b")
		let state = boardReducer(initialBoardState, { type: "select", scope: a })
		state = boardReducer(state, { type: "event", event: readyEvent(a) })
		state = boardReducer(state, { type: "event", event: readyEvent(b) })

		expect(state.selectedBoardId).toBe(a.id)
		expect(state.boards[a.id]?.snapshot?.scope.id).toBe(a.id)
		expect(state.boards[b.id]?.snapshot?.scope.id).toBe(b.id)
	})

	it("keeps a dirty editor separate from updates and requires switch resolution", () => {
		const a = scope("a")
		const b = scope("b")
		let state = boardReducer(initialBoardState, { type: "select", scope: a })
		state = boardReducer(state, { type: "event", event: readyEvent(a) })
		state = boardReducer(state, {
			type: "edit_draft",
			boardId: a.id,
			draft: { dirty: true, values: { title: "Unsaved title" } },
		})
		state = boardReducer(state, { type: "event", event: readyEvent(a, 2) })
		state = boardReducer(state, { type: "select", scope: b })

		expect(state.selectedBoardId).toBe(a.id)
		expect(state.pendingSelection).toEqual(b)
		expect(state.boards[a.id]?.draft?.values.title).toBe("Unsaved title")

		state = boardReducer(state, { type: "resolve_selection", resolution: "preserve" })
		expect(state.selectedBoardId).toBe(b.id)
		expect(state.boards[a.id]?.draft?.values.title).toBe("Unsaved title")
	})
})

describe("BoardStateContextProvider", () => {
	it("applies simulated extension messages with consistent empty and ready states", () => {
		const selected = scope("a")
		const Probe = () => {
			const board = useBoardState()
			return (
				<button onClick={() => board.dispatch({ type: "select", scope: selected })}>
					{board.status}:{board.selectedBoard?.snapshot?.activeTickets[0]?.statementOfWork.title ?? "none"}
				</button>
			)
		}
		render(
			<BoardStateContextProvider>
				<Probe />
			</BoardStateContextProvider>,
		)
		expect(screen.getByRole("button")).toHaveTextContent("empty:none")
		act(() => screen.getByRole("button").click())
		expect(screen.getByRole("button")).toHaveTextContent("loading:none")
		act(() => window.dispatchEvent(new MessageEvent("message", { data: readyEvent(selected) })))
		expect(screen.getByRole("button")).toHaveTextContent("ready:A ticket")
	})
})
