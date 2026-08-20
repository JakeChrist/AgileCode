import React, { createContext, useContext, useEffect, useMemo, useReducer } from "react"

import {
	boardResultSchema,
	boardScopesEventSchema,
	boardStateEventSchema,
	type BoardError,
	type BoardResult,
	type BoardScope,
	type BoardSnapshot,
	type BoardStateEvent,
	type TicketStatementOfWork,
} from "@roo-code/types"

export type BoardViewStatus = "empty" | "loading" | "uninitialized" | "ready" | "error"

export interface TicketEditorDraft {
	ticketId?: string
	values: Partial<TicketStatementOfWork>
	dirty: boolean
}

export interface BoardEntry {
	scope: BoardScope
	status: Exclude<BoardViewStatus, "empty">
	revision: number
	snapshot?: BoardSnapshot
	error?: BoardError
	diagnostics: string[]
	draft?: TicketEditorDraft
	lastResult?: BoardResult
}

export interface BoardState {
	selectedBoardId?: string
	boards: Record<string, BoardEntry>
	pendingSelection?: BoardScope
	availableScopes: BoardScope[]
	unavailableBoardId?: string
}

export type BoardAction =
	| { type: "select"; scope: BoardScope }
	| { type: "resolve_selection"; resolution: "preserve" | "discard" | "cancel" }
	| { type: "event"; event: BoardStateEvent }
	| { type: "result"; result: BoardResult }
	| { type: "edit_draft"; boardId: string; draft: TicketEditorDraft }
	| { type: "clear_draft"; boardId: string }
	| { type: "scopes"; scopes: BoardScope[]; selectedBoardId?: string; unavailableBoardId?: string }

export const initialBoardState: BoardState = { boards: {}, availableScopes: [] }

const selectImmediately = (state: BoardState, scope: BoardScope): BoardState => ({
	...state,
	selectedBoardId: scope.id,
	pendingSelection: undefined,
	boards: {
		...state.boards,
		[scope.id]: state.boards[scope.id] ?? { scope, status: "loading", revision: 0, diagnostics: [] },
	},
})

function applyResult(entry: BoardEntry, result: BoardResult): BoardEntry {
	if (!result.ok) return { ...entry, error: result.error, lastResult: result }
	if (result.operation === "load_board") {
		return { ...entry, scope: result.snapshot.scope, status: "ready", snapshot: result.snapshot, error: undefined }
	}
	if (!entry.snapshot) return entry

	if (result.operation === "update_board_settings") {
		return { ...entry, snapshot: { ...entry.snapshot, settings: result.settings } }
	}
	if (result.operation === "reorder_tickets") {
		return { ...entry, lastResult: result, snapshot: { ...entry.snapshot, board: result.board } }
	}
	if (result.operation === "delete_ticket") {
		return {
			...entry,
			snapshot: {
				...entry.snapshot,
				board: result.board,
				activeTickets: entry.snapshot.activeTickets.filter(({ id }) => id !== result.ticketId),
				archivedTickets: entry.snapshot.archivedTickets.filter(({ id }) => id !== result.ticketId),
			},
		}
	}
	if (!("ticket" in result)) return entry

	const activeTickets = entry.snapshot.activeTickets.filter(({ id }) => id !== result.ticket.id)
	const archivedTickets = entry.snapshot.archivedTickets.filter(({ id }) => id !== result.ticket.id)
	if (result.ticket.lifecycle.state === "archived") archivedTickets.push(result.ticket)
	else activeTickets.push(result.ticket)
	return {
		...entry,
		lastResult: result,
		snapshot: {
			...entry.snapshot,
			activeTickets,
			archivedTickets,
			...("board" in result ? { board: result.board } : {}),
		},
	}
}

export function boardReducer(state: BoardState, action: BoardAction): BoardState {
	switch (action.type) {
		case "select": {
			if (action.scope.id === state.selectedBoardId) return state
			const currentDraft = state.selectedBoardId ? state.boards[state.selectedBoardId]?.draft : undefined
			if (currentDraft?.dirty) return { ...state, pendingSelection: action.scope }
			return selectImmediately(state, action.scope)
		}
		case "resolve_selection": {
			if (!state.pendingSelection || action.resolution === "cancel")
				return { ...state, pendingSelection: undefined }
			let next = state
			if (action.resolution === "discard" && state.selectedBoardId) {
				const current = state.boards[state.selectedBoardId]
				if (current)
					next = {
						...state,
						boards: { ...state.boards, [state.selectedBoardId]: { ...current, draft: undefined } },
					}
			}
			return selectImmediately(next, state.pendingSelection)
		}
		case "event": {
			const { event } = action
			const previous = state.boards[event.boardId]
			if (previous && event.revision < previous.revision) return state
			const selectsBoard = event.status === "loading" || state.selectedBoardId === undefined
			const entry: BoardEntry = {
				scope: event.scope,
				status: event.status,
				revision: event.revision,
				diagnostics: event.status === "error" ? event.diagnostics : [],
				// Never retain another load's tickets while loading, uninitialized, or failed.
				snapshot: event.status === "ready" ? event.snapshot : undefined,
				error: event.status === "error" ? event.error : undefined,
				draft: previous?.draft,
			}
			return {
				...state,
				// Board state events are authoritative for the repository currently
				// selected by the extension host, including selection changes.
				selectedBoardId: selectsBoard ? event.boardId : state.selectedBoardId,
				pendingSelection: selectsBoard ? undefined : state.pendingSelection,
				boards: { ...state.boards, [event.boardId]: entry },
			}
		}
		case "result": {
			const entry = state.boards[action.result.boardId]
			if (!entry) return state
			return { ...state, boards: { ...state.boards, [action.result.boardId]: applyResult(entry, action.result) } }
		}
		case "edit_draft": {
			const entry = state.boards[action.boardId]
			if (!entry) return state
			return { ...state, boards: { ...state.boards, [action.boardId]: { ...entry, draft: action.draft } } }
		}
		case "clear_draft": {
			const entry = state.boards[action.boardId]
			if (!entry) return state
			return { ...state, boards: { ...state.boards, [action.boardId]: { ...entry, draft: undefined } } }
		}
		case "scopes":
			return {
				...state,
				availableScopes: action.scopes,
				selectedBoardId: action.selectedBoardId,
				unavailableBoardId: action.unavailableBoardId,
			}
	}
}

export interface BoardStateContextValue {
	state: BoardState
	selectedBoard?: BoardEntry
	status: BoardViewStatus
	dispatch: React.Dispatch<BoardAction>
}

const BoardStateContext = createContext<BoardStateContextValue | undefined>(undefined)

export const BoardStateContextProvider = ({ children }: { children: React.ReactNode }) => {
	const [state, dispatch] = useReducer(boardReducer, initialBoardState)

	useEffect(() => {
		const onMessage = ({ data }: MessageEvent) => {
			const scopesEvent = boardScopesEventSchema.safeParse(data)
			if (scopesEvent.success) {
				const { scopes, selectedBoardId, unavailableBoardId } = scopesEvent.data
				return dispatch({ type: "scopes", scopes, selectedBoardId, unavailableBoardId })
			}
			const event = boardStateEventSchema.safeParse(data)
			if (event.success) return dispatch({ type: "event", event: event.data })
			if (data?.type === "board_result") {
				const result = boardResultSchema.safeParse(data.result)
				if (result.success) dispatch({ type: "result", result: result.data })
			}
		}
		window.addEventListener("message", onMessage)
		return () => window.removeEventListener("message", onMessage)
	}, [])

	const value = useMemo<BoardStateContextValue>(() => {
		const selectedBoard = state.selectedBoardId ? state.boards[state.selectedBoardId] : undefined
		return { state, selectedBoard, status: selectedBoard?.status ?? "empty", dispatch }
	}, [state])

	return <BoardStateContext.Provider value={value}>{children}</BoardStateContext.Provider>
}

export const useBoardState = () => {
	const context = useContext(BoardStateContext)
	if (!context) throw new Error("useBoardState must be used within BoardStateContextProvider")
	return context
}
