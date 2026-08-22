import { useEffect, useId, useRef, useState, type DragEvent } from "react"

import { activeBoardStates, getTicketStatementOfWorkLock } from "@roo-code/types"

import { useBoardState } from "@/context/BoardStateContext"
import { useExtensionState } from "@/context/ExtensionStateContext"
import { vscode } from "@/utils/vscode"

import TicketCard from "./TicketCard"
import TicketAuthoringForm from "./TicketAuthoringForm"
import TicketDetailView from "./TicketDetailView"
import { manualTicketTransitions } from "./ticketTransitions"

const columnLabels: Record<(typeof activeBoardStates)[number], string> = {
	backlog: "Backlog",
	ready: "Ready",
	in_progress: "In Progress",
	blocked: "Blocked",
	review: "Review",
	done: "Done",
}

const emptyColumnGuidance: Record<(typeof activeBoardStates)[number], string> = {
	backlog: "No tickets have been prioritized yet.",
	ready: "No tickets are ready to begin.",
	in_progress: "No tickets are currently executing.",
	blocked: "No tickets are waiting on a blocker.",
	review: "No tickets are awaiting your acceptance or rejection.",
	done: "No tickets have been accepted yet.",
}

const columnGuidance: Record<(typeof activeBoardStates)[number], string> = {
	backlog: "Tickets captured for future prioritization.",
	ready: "Tickets prepared and ready to begin.",
	in_progress: "Tickets currently executing.",
	blocked: "Tickets here are blocked before execution or paused with context that can be resumed.",
	review: "Tickets here await your acceptance or rejection.",
	done: "Tickets you have accepted as complete.",
}

const compactBoardQuery = "(max-width: 600px)"
const draftFingerprint = (draft?: { values: unknown; roughRequest?: string }) =>
	JSON.stringify({ values: draft?.values ?? {}, roughRequest: draft?.roughRequest ?? "" })
type ActiveBoardState = (typeof activeBoardStates)[number]

const scopeLabel = (scope: { kind: "git" | "workspace"; rootPath: string }) => {
	const normalized = scope.rootPath.replace(/[\\/]+$/, "")
	const segments = normalized.split(/[\\/]/).filter(Boolean)
	const leaf = segments.at(-1) ?? normalized
	const parent = segments.slice(0, -1).join("/")
	const kind = scope.kind === "git" ? "Git repository" : "Workspace folder (non-Git)"
	return `${leaf}${parent ? ` — ${parent}` : ""} (${kind})`
}

const useCompactBoard = () => {
	const [compact, setCompact] = useState(() => window.matchMedia?.(compactBoardQuery).matches ?? false)

	useEffect(() => {
		const query = window.matchMedia?.(compactBoardQuery)
		if (!query) return
		const update = () => setCompact(query.matches)
		update()
		query.addEventListener("change", update)
		return () => query.removeEventListener("change", update)
	}, [])

	return compact
}

const BoardView = () => {
	const { cwd, renderContext } = useExtensionState()
	const { selectedBoard, status, state, dispatch } = useBoardState()
	const availableScopes = state?.availableScopes ?? []
	const snapshot = selectedBoard?.snapshot
	const ticketsById = new Map(snapshot?.activeTickets.map((ticket) => [ticket.id, ticket]))
	const compact = useCompactBoard()
	const [selectedColumn, setSelectedColumn] = useState<ActiveBoardState>(activeBoardStates[0])
	const [draggedTicket, setDraggedTicket] = useState<{ id: string; source: ActiveBoardState } | null>(null)
	const [selectedTicketId, setSelectedTicketId] = useState<string | null>(null)
	const [announcement, setAnnouncement] = useState("")
	const [authoring, setAuthoring] = useState(false)
	const [createRequestId, setCreateRequestId] = useState<string>()
	const [improvementAttempt, setImprovementAttempt] = useState<{
		requestId: string
		boardId: string
		draftFingerprint: string
		obsolete: boolean
	}>()
	const [editingTicketId, setEditingTicketId] = useState<string>()
	const [editRequestId, setEditRequestId] = useState<string>()
	const [executionRequest, setExecutionRequest] = useState<{ ticketId: string; requestId: string }>()
	const pendingFocus = useRef<{ ticketId: string; column: ActiveBoardState } | null>(null)
	const returnFocusTicket = useRef<string | null>(null)
	const previousSnapshot = useRef(snapshot)
	const visibleColumns = compact ? [selectedColumn] : activeBoardStates
	const guidanceIdPrefix = useId()
	const draggedTicketModel = draggedTicket ? ticketsById.get(draggedTicket.id) : undefined
	const draggedTicketSource = draggedTicket?.source
	const validDropDestinations = new Set<ActiveBoardState>(
		draggedTicketModel && draggedTicketSource
			? [
					draggedTicketSource,
					...manualTicketTransitions(draggedTicketModel).map(({ destination }) => destination),
				]
			: [],
	)
	const requestBoard = (operation: "load_board" | "initialize_board") => {
		if (!selectedBoard) return
		vscode.postMessage({
			type: "board_request",
			request: { requestId: `${operation}-${Date.now()}`, boardId: selectedBoard.scope.id, operation },
		} as never)
	}
	const selectScope = (scopeId: string) => {
		const scope = availableScopes.find(({ id }) => id === scopeId)
		if (!scope || scope.id === state.selectedBoardId) return false
		setImprovementAttempt((attempt) => (attempt ? { ...attempt, obsolete: true } : attempt))

		if (selectedBoard?.draft?.dirty) {
			const preserve = window.confirm(
				"This board has unsaved ticket input. Switch boards and preserve the draft for when you return?",
			)
			if (!preserve) return false
			dispatch({ type: "select", scope })
			dispatch({ type: "resolve_selection", resolution: "preserve" })
		} else {
			dispatch({ type: "select", scope })
		}
		// The extension host validates the identity, persists the explicit choice,
		// and publishes a fresh authoritative snapshot for it.
		vscode.postMessage({ type: "select_board_scope", scope } as never)
		return true
	}

	useEffect(() => {
		if (
			improvementAttempt &&
			!improvementAttempt.obsolete &&
			selectedBoard?.scope.id !== improvementAttempt.boardId
		)
			setImprovementAttempt({ ...improvementAttempt, obsolete: true })
	}, [selectedBoard?.scope.id, improvementAttempt])

	const moveTicket = (ticketId: string, destination: ActiveBoardState) => {
		if (!selectedBoard) return
		const ticket = ticketsById.get(ticketId)
		const transition = ticket && manualTicketTransitions(ticket).find((item) => item.destination === destination)
		if (!transition) {
			setAnnouncement(`${ticketId} cannot be moved to ${columnLabels[destination]} in its current condition.`)
			return
		}
		if (transition.operation === "start_ticket_execution") {
			performTicketAction(transition.operation, ticketId)
			return
		}
		pendingFocus.current = { ticketId, column: destination }
		setAnnouncement(`Moving ${ticketId} to ${columnLabels[destination]}.`)
		vscode.postMessage({
			type: "board_request",
			request: {
				requestId: `move-${ticketId}-${Date.now()}`,
				boardId: selectedBoard.scope.id,
				operation: "move_ticket",
				ticketId,
				destination,
				position: snapshot?.board.columns[destination].length ?? 0,
			},
		} as never)
	}
	const reorderTickets = (column: ActiveBoardState, ticketId: string, position: number) => {
		if (!selectedBoard || !snapshot) return
		const expectedOrder = snapshot.board.columns[column]
		const currentPosition = expectedOrder.indexOf(ticketId)
		if (currentPosition < 0) return
		const orderedIds = [...expectedOrder]
		orderedIds.splice(currentPosition, 1)
		orderedIds.splice(Math.max(0, Math.min(position, orderedIds.length)), 0, ticketId)
		if (orderedIds.every((id, index) => id === expectedOrder[index])) return
		setAnnouncement(`Reordering ${ticketId} in ${columnLabels[column]}.`)
		vscode.postMessage({
			type: "board_request",
			request: {
				requestId: `reorder-${ticketId}-${Date.now()}`,
				boardId: selectedBoard.scope.id,
				operation: "reorder_tickets",
				state: column,
				orderedIds,
				expectedOrder,
			},
		} as never)
	}

	const performTicketAction = (
		operation: string,
		ticketId: string,
		destination?: ActiveBoardState,
		comment?: string,
	) => {
		if (!selectedBoard) return
		if (operation === "start_ticket_execution" && executionRequest?.ticketId === ticketId) return
		const ticket = ticketsById.get(ticketId)
		const currentColumn = ticket?.lifecycle.state === "archived" ? "done" : ticket?.lifecycle.state
		pendingFocus.current = { ticketId, column: destination ?? (currentColumn as ActiveBoardState) }
		setAnnouncement(`${operation.replaceAll("_", " ")} requested for ${ticketId}.`)
		const requestId = `${operation}-${ticketId}-${Date.now()}`
		if (operation === "start_ticket_execution") setExecutionRequest({ ticketId, requestId })
		vscode.postMessage({
			type: "board_request",
			request: {
				requestId,
				boardId: selectedBoard.scope.id,
				operation,
				ticketId,
				...(destination ? { destination, position: snapshot?.board.columns[destination].length ?? 0 } : {}),
				...(comment ? { comment } : {}),
			},
		} as never)
	}

	useEffect(() => {
		if (!executionRequest || selectedBoard?.lastResult?.requestId !== executionRequest.requestId) return
		const result = selectedBoard.lastResult
		setExecutionRequest(undefined)
		setAnnouncement(
			result.ok
				? `Execution started for ${executionRequest.ticketId}.`
				: `Execution could not start for ${executionRequest.ticketId}: ${result.error.message}`,
		)
	}, [executionRequest, selectedBoard?.lastResult])

	const dropTicket = (event: DragEvent, destination: ActiveBoardState) => {
		event.preventDefault()
		if (!draggedTicket || !validDropDestinations.has(destination)) {
			if (draggedTicket)
				setAnnouncement(
					`${draggedTicket.id} cannot be moved to ${columnLabels[destination]} in its current condition.`,
				)
			setDraggedTicket(null)
			return
		}
		if (draggedTicket?.source === destination) {
			reorderTickets(destination, draggedTicket.id, snapshot?.board.columns[destination].length ?? 0)
		} else if (draggedTicket) moveTicket(draggedTicket.id, destination)
		setDraggedTicket(null)
	}

	const allTickets = [...(snapshot?.activeTickets ?? []), ...(snapshot?.archivedTickets ?? [])]
	const selectedTicket = allTickets.find(({ id }) => id === selectedTicketId)
	const openTicket = (ticketId: string) => {
		returnFocusTicket.current = ticketId
		setSelectedTicketId(ticketId)
	}
	const closeTicket = () => {
		setSelectedTicketId(null)
	}
	useEffect(() => {
		if (!createRequestId || selectedBoard?.lastResult?.requestId !== createRequestId) return
		if (selectedBoard.lastResult.ok) {
			dispatch({ type: "clear_draft", boardId: selectedBoard.scope.id })
			setAuthoring(false)
			setCreateRequestId(undefined)
		}
	}, [createRequestId, selectedBoard?.lastResult, selectedBoard?.scope.id, dispatch])
	useEffect(() => {
		if (!editRequestId || selectedBoard?.lastResult?.requestId !== editRequestId) return
		if (selectedBoard.lastResult.ok) {
			setEditingTicketId(undefined)
			setEditRequestId(undefined)
		}
	}, [editRequestId, selectedBoard?.lastResult])

	useEffect(() => {
		if (selectedTicketId) return
		const ticketId = returnFocusTicket.current
		if (ticketId)
			Array.from(document.querySelectorAll<HTMLElement>("[data-ticket-id]"))
				.find((element) => element.dataset.ticketId === ticketId)
				?.focus()
	}, [selectedTicketId])

	useEffect(() => {
		if (previousSnapshot.current === snapshot) return
		previousSnapshot.current = snapshot
		const pending = pendingFocus.current
		if (!pending) return
		pendingFocus.current = null
		setTimeout(() => {
			const ticket = Array.from(document.querySelectorAll<HTMLElement>("[data-ticket-id]")).find(
				(element) => element.dataset.ticketId === pending.ticketId,
			)
			const fallback = document.querySelector<HTMLElement>(`[data-column="${pending.column}"]`)
			;(ticket ?? fallback)?.focus()
			setAnnouncement(
				ticket
					? `${pending.ticketId} is now ${ticket.getAttribute("aria-describedby") ? "updated" : "available"}.`
					: `${pending.ticketId} was removed from the active board.`,
			)
		})
	}, [snapshot])

	return (
		<main className="relative flex h-full min-h-0 flex-col bg-vscode-editor-background" data-testid="board-view">
			<div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
				{announcement}
			</div>
			<header className="border-b border-vscode-panel-border px-5 py-4">
				<div className="flex items-center justify-between gap-4">
					<h1 className="m-0 text-lg font-semibold text-vscode-foreground">Board</h1>
					{status === "ready" && selectedBoard && (
						<button
							className="rounded bg-vscode-button-background px-3 py-1.5 text-vscode-button-foreground"
							onClick={() => setAuthoring(true)}>
							Write Ticket
						</button>
					)}
					{renderContext !== "editor" && selectedBoard && (
						<button
							className="shrink-0 rounded bg-vscode-button-background px-3 py-1.5 text-vscode-button-foreground"
							onClick={() =>
								vscode.postMessage({
									type: "open_full_board",
									boardId: selectedBoard.scope.id,
								} as never)
							}>
							Open Full Board
						</button>
					)}
					{availableScopes.length > 0 && (
						<select
							aria-label="Board scope"
							className="min-w-0 max-w-64 rounded border border-vscode-dropdown-border bg-vscode-dropdown-background px-2 py-1 text-vscode-dropdown-foreground"
							value={state?.selectedBoardId ?? ""}
							onChange={(event) => {
								if (!selectScope(event.target.value)) event.target.value = state.selectedBoardId ?? ""
							}}>
							<option value="" disabled>
								Select a board
							</option>
							{availableScopes.map((scope) => (
								<option key={scope.id} value={scope.id}>
									{scopeLabel(scope)}
								</option>
							))}
						</select>
					)}
				</div>
				<p
					className="m-0 mt-1 truncate text-sm text-vscode-descriptionForeground"
					title={selectedBoard?.scope.rootPath ?? cwd}>
					{selectedBoard
						? scopeLabel(selectedBoard.scope)
						: cwd || "Open a repository or workspace folder to view its board"}
				</p>
			</header>

			{status === "empty" && (
				<div className="m-auto px-6 text-center text-vscode-descriptionForeground">
					{state?.unavailableBoardId
						? "The previously selected board is unavailable. Select an available board to continue."
						: cwd
							? "No board is available for this repository."
							: "Open a repository to get started."}
				</div>
			)}
			{status === "loading" && <div className="m-auto text-vscode-descriptionForeground">Loading board…</div>}
			{status === "uninitialized" && (
				<div className="m-auto max-w-lg px-6 text-center">
					<h2 className="text-base font-semibold text-vscode-foreground">
						Start a board for this repository
					</h2>
					<p className="text-vscode-descriptionForeground">
						No ticket store exists yet. Initialize an empty board here, or return to Chat to continue
						without one.
					</p>
					<button
						className="rounded bg-vscode-button-background px-3 py-2 text-vscode-button-foreground"
						onClick={() => requestBoard("initialize_board")}>
						Initialize board
					</button>
				</div>
			)}
			{status === "error" && (
				<div className="m-auto max-w-lg px-6 text-center">
					<h2 className="text-base font-semibold text-vscode-errorForeground">Board unavailable</h2>
					<p className="text-vscode-errorForeground">
						{selectedBoard?.error?.message ?? "The board could not be loaded."}
					</p>
					<p className="text-sm text-vscode-descriptionForeground">
						No files were changed. Retry the authoritative load, or check the listed records and extension
						logs.
					</p>
					{selectedBoard?.diagnostics?.length ? (
						<ul
							aria-label="Load diagnostics"
							className="text-left text-sm text-vscode-descriptionForeground">
							{selectedBoard.diagnostics.map((item) => (
								<li key={item}>{item}</li>
							))}
						</ul>
					) : null}
					{selectedBoard?.error?.retryable && (
						<button
							className="rounded bg-vscode-button-background px-3 py-2 text-vscode-button-foreground"
							onClick={() => requestBoard("load_board")}>
							Retry
						</button>
					)}
				</div>
			)}
			{status === "ready" && snapshot && (
				<div className="flex min-h-0 flex-1 flex-col">
					{(snapshot.diagnostics?.length ?? 0) > 0 && (
						<aside
							role="alert"
							className="mx-4 mt-3 rounded border border-vscode-inputValidation-warningBorder p-3 text-sm text-vscode-foreground">
							<strong>Some records could not be loaded.</strong>
							<ul className="mb-0 mt-1">
								{snapshot.diagnostics?.map(({ record, problem }) => (
									<li key={`${record}-${problem}`}>
										<code>{record}</code>: {problem}
									</li>
								))}
							</ul>
						</aside>
					)}
					{snapshot.activeTickets.length === 0 && snapshot.archivedTickets.length === 0 && (
						<div role="status" className="px-4 pt-3 text-sm text-vscode-descriptionForeground">
							This board is empty. Create the first ticket to start planning.
						</div>
					)}
					{compact && (
						<label className="shrink-0 px-4 pt-3 text-xs font-medium text-vscode-descriptionForeground">
							Workflow column
							<select
								aria-label="Workflow column"
								className="mt-1 w-full rounded border border-vscode-dropdown-border bg-vscode-dropdown-background px-2 py-2 text-sm text-vscode-dropdown-foreground"
								value={selectedColumn}
								onChange={(event) => setSelectedColumn(event.target.value as ActiveBoardState)}>
								{activeBoardStates.map((column) => (
									<option key={column} value={column}>
										{columnLabels[column]} ({snapshot.board.columns[column].length})
									</option>
								))}
							</select>
						</label>
					)}
					<div
						className={`flex min-h-0 flex-1 gap-3 p-4 ${compact ? "overflow-hidden" : "overflow-x-auto overflow-y-hidden"}`}
						aria-label="Kanban board">
						{visibleColumns.map((column) => (
							<section
								key={column}
								aria-label={`${columnLabels[column]} column`}
								aria-describedby={`${guidanceIdPrefix}-${column}`}
								data-column={column}
								aria-disabled={draggedTicket ? !validDropDestinations.has(column) : undefined}
								tabIndex={0}
								onDragOver={(event) => {
									if (!validDropDestinations.has(column)) return
									event.preventDefault()
									event.dataTransfer.dropEffect = "move"
								}}
								onDrop={(event) => dropTicket(event, column)}
								className={`flex h-full min-h-0 flex-col rounded border bg-vscode-sideBar-background transition-colors ${
									draggedTicket
										? validDropDestinations.has(column)
											? "border-vscode-focusBorder"
											: "cursor-not-allowed border-vscode-panel-border opacity-50"
										: "border-vscode-panel-border"
								} ${compact ? "w-full min-w-0" : "w-72 min-w-72"}`}>
								<header className="shrink-0 border-b border-vscode-panel-border px-3 py-3">
									<h2 className="m-0 shrink-0 text-sm font-semibold text-vscode-foreground">
										{columnLabels[column]}{" "}
										<span
											aria-label={`${snapshot.board.columns[column].length} tickets`}
											className="text-vscode-descriptionForeground">
											{snapshot.board.columns[column].length}
										</span>
									</h2>
									<p
										id={`${guidanceIdPrefix}-${column}`}
										role="status"
										className="m-0 mt-1 text-xs text-vscode-descriptionForeground">
										{column === "in_progress" &&
										draggedTicket?.source !== "in_progress" &&
										validDropDestinations.has(column)
											? "Drop to execute or resume this ticket."
											: draggedTicket && !validDropDestinations.has(column)
												? "This ticket cannot be dropped here."
												: columnGuidance[column]}
									</p>
								</header>
								<div
									className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3"
									aria-label={`${columnLabels[column]} tickets`}>
									{snapshot.board.columns[column].length === 0 && (
										<p className="m-auto px-2 text-center text-sm text-vscode-descriptionForeground">
											{emptyColumnGuidance[column]}
										</p>
									)}
									{snapshot.board.columns[column].map((ticketId, index) => {
										const ticket = ticketsById.get(ticketId)
										return ticket ? (
											<div
												key={ticketId}
												draggable
												onDragOver={(event) => {
													if (draggedTicket?.source === column) event.preventDefault()
												}}
												onDrop={(event) => {
													if (draggedTicket?.source !== column) return
													event.preventDefault()
													event.stopPropagation()
													reorderTickets(column, draggedTicket.id, index)
													setDraggedTicket(null)
												}}
												onDragStart={(event) => {
													event.dataTransfer.effectAllowed = "move"
													event.dataTransfer.setData("text/plain", ticketId)
													setDraggedTicket({ id: ticketId, source: column })
												}}
												onDragEnd={() => setDraggedTicket(null)}
												className={`min-w-0 ${draggedTicket?.id === ticketId ? "opacity-50" : ""}`}>
												<TicketCard
													ticket={ticket}
													tickets={[...snapshot.activeTickets, ...snapshot.archivedTickets]}
													column={column}
													onAction={performTicketAction}
													onOpen={openTicket}
													starting={executionRequest?.ticketId === ticket.id}
												/>
												{snapshot.board.columns[column].length > 1 && (
													<div className="mt-1 flex justify-end gap-1">
														<button
															aria-label={`Move ${ticketId} up`}
															disabled={index === 0}
															onClick={() => reorderTickets(column, ticketId, index - 1)}
															className="rounded px-2 text-vscode-descriptionForeground disabled:opacity-40">
															↑
														</button>
														<button
															aria-label={`Move ${ticketId} down`}
															disabled={
																index === snapshot.board.columns[column].length - 1
															}
															onClick={() => reorderTickets(column, ticketId, index + 1)}
															className="rounded px-2 text-vscode-descriptionForeground disabled:opacity-40">
															↓
														</button>
													</div>
												)}
											</div>
										) : null
									})}
								</div>
							</section>
						))}
					</div>
					{snapshot.archivedTickets.length > 0 && (
						<footer className="shrink-0 border-t border-vscode-panel-border px-4 py-2 text-xs text-vscode-descriptionForeground">
							Archived:{" "}
							{snapshot.archivedTickets.map((ticket) => (
								<span key={ticket.id}>
									<button
										className="ml-2 text-vscode-textLink-foreground hover:underline"
										aria-label={`Open archived ticket ${ticket.id}`}
										onClick={() => openTicket(ticket.id)}>
										{ticket.id} details
									</button>
									<button
										className="ml-2 text-vscode-textLink-foreground hover:underline"
										aria-label={`Restore ${ticket.id}`}
										onClick={() => performTicketAction("restore_ticket", ticket.id)}>
										Restore
									</button>
								</span>
							))}
						</footer>
					)}
				</div>
			)}
			{selectedTicket && (
				<TicketDetailView
					ticket={selectedTicket}
					tickets={allTickets}
					onBack={closeTicket}
					onOpenTicket={openTicket}
					onEdit={() => setEditingTicketId(selectedTicket.id)}
				/>
			)}
			{editingTicketId &&
				selectedBoard &&
				selectedTicket?.id === editingTicketId &&
				!getTicketStatementOfWorkLock(selectedTicket).locked && (
					<TicketAuthoringForm
						mode="edit"
						initialValues={selectedTicket.statementOfWork}
						submitting={!!editRequestId && selectedBoard.lastResult?.requestId !== editRequestId}
						submitError={
							editRequestId &&
							selectedBoard.lastResult?.requestId === editRequestId &&
							!selectedBoard.lastResult.ok
								? selectedBoard.lastResult.error.message
								: undefined
						}
						onChange={() => undefined}
						onCancel={() => {
							setEditingTicketId(undefined)
							setEditRequestId(undefined)
						}}
						onSubmit={(statementOfWork) => {
							if (editRequestId && selectedBoard.lastResult?.requestId !== editRequestId) return
							const requestId = `update-${crypto.randomUUID()}`
							setEditRequestId(requestId)
							vscode.postMessage({
								type: "board_request",
								request: {
									requestId,
									boardId: selectedBoard.scope.id,
									operation: "update_ticket",
									ticketId: editingTicketId,
									statementOfWork,
								},
							} as never)
						}}
					/>
				)}
			{authoring && selectedBoard && (
				<TicketAuthoringForm
					onImprove={(roughRequest) => {
						if (
							improvementAttempt &&
							!improvementAttempt.obsolete &&
							selectedBoard.lastResult?.requestId !== improvementAttempt.requestId
						)
							return
						const requestId = `improve-${crypto.randomUUID()}`
						setImprovementAttempt({
							requestId,
							boardId: selectedBoard.scope.id,
							draftFingerprint: draftFingerprint(selectedBoard.draft ?? { values: {}, roughRequest }),
							obsolete: false,
						})
						vscode.postMessage({
							type: "board_request",
							request: {
								requestId,
								boardId: selectedBoard.scope.id,
								operation: "improve_ticket_draft",
								roughRequest,
							},
						} as never)
					}}
					roughRequest={selectedBoard.draft?.roughRequest ?? ""}
					onRoughRequestChange={(roughRequest) =>
						dispatch({
							type: "edit_draft",
							boardId: selectedBoard.scope.id,
							draft: { values: selectedBoard.draft?.values ?? {}, roughRequest, dirty: true },
						})
					}
					improving={
						!!improvementAttempt &&
						!improvementAttempt.obsolete &&
						improvementAttempt.boardId === selectedBoard.scope.id &&
						selectedBoard.lastResult?.requestId !== improvementAttempt.requestId
					}
					improvementDraft={
						improvementAttempt &&
						!improvementAttempt.obsolete &&
						improvementAttempt.boardId === selectedBoard.scope.id &&
						improvementAttempt.draftFingerprint === draftFingerprint(selectedBoard.draft) &&
						selectedBoard.lastResult?.requestId === improvementAttempt.requestId &&
						selectedBoard.lastResult.ok &&
						selectedBoard.lastResult.operation === "improve_ticket_draft"
							? selectedBoard.lastResult.draft
							: undefined
					}
					improvementError={
						improvementAttempt &&
						!improvementAttempt.obsolete &&
						improvementAttempt.boardId === selectedBoard.scope.id &&
						selectedBoard.lastResult?.requestId === improvementAttempt.requestId &&
						!selectedBoard.lastResult.ok
							? selectedBoard.lastResult.error.message
							: undefined
					}
					submitting={!!createRequestId && selectedBoard.lastResult?.requestId !== createRequestId}
					submitError={
						createRequestId &&
						selectedBoard.lastResult?.requestId === createRequestId &&
						!selectedBoard.lastResult.ok
							? selectedBoard.lastResult.error.message
							: undefined
					}
					initialValues={selectedBoard.draft?.values}
					onChange={(values) =>
						dispatch({
							type: "edit_draft",
							boardId: selectedBoard.scope.id,
							draft: { values, roughRequest: selectedBoard.draft?.roughRequest, dirty: true },
						})
					}
					onCancel={() => {
						dispatch({ type: "clear_draft", boardId: selectedBoard.scope.id })
						setAuthoring(false)
					}}
					onSubmit={(ticket) => {
						if (createRequestId && selectedBoard.lastResult?.requestId !== createRequestId) return
						const requestId = `create-${crypto.randomUUID()}`
						setCreateRequestId(requestId)
						vscode.postMessage({
							type: "board_request",
							request: {
								requestId,
								boardId: selectedBoard.scope.id,
								operation: "create_ticket",
								ticket,
							},
						} as never)
					}}
				/>
			)}
		</main>
	)
}

export default BoardView
