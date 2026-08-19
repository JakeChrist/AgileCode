import { useEffect, useId, useState, type DragEvent } from "react"

import { activeBoardStates } from "@roo-code/types"

import { useBoardState } from "@/context/BoardStateContext"
import { useExtensionState } from "@/context/ExtensionStateContext"
import { vscode } from "@/utils/vscode"

import TicketCard from "./TicketCard"
import TicketDetailView from "./TicketDetailView"

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
type ActiveBoardState = (typeof activeBoardStates)[number]

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
	const { cwd } = useExtensionState()
	const { selectedBoard, status, state } = useBoardState()
	const availableScopes = state?.availableScopes ?? []
	const snapshot = selectedBoard?.snapshot
	const ticketsById = new Map(snapshot?.activeTickets.map((ticket) => [ticket.id, ticket]))
	const compact = useCompactBoard()
	const [selectedColumn, setSelectedColumn] = useState<ActiveBoardState>(activeBoardStates[0])
	const [draggedTicket, setDraggedTicket] = useState<{ id: string; source: ActiveBoardState } | null>(null)
	const [selectedTicketId, setSelectedTicketId] = useState<string | null>(null)
	const visibleColumns = compact ? [selectedColumn] : activeBoardStates
	const guidanceIdPrefix = useId()
	const requestBoard = (operation: "load_board" | "initialize_board") => {
		if (!selectedBoard) return
		vscode.postMessage({
			type: "board_request",
			request: { requestId: `${operation}-${Date.now()}`, boardId: selectedBoard.scope.id, operation },
		} as never)
	}

	const moveTicket = (ticketId: string, destination: ActiveBoardState) => {
		if (!selectedBoard) return
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

	const performTicketAction = (operation: string, ticketId: string, destination?: ActiveBoardState) => {
		if (!selectedBoard) return
		vscode.postMessage({
			type: "board_request",
			request: {
				requestId: `${operation}-${ticketId}-${Date.now()}`,
				boardId: selectedBoard.scope.id,
				operation,
				ticketId,
				...(destination ? { destination, position: snapshot?.board.columns[destination].length ?? 0 } : {}),
			},
		} as never)
	}

	const dropTicket = (event: DragEvent, destination: ActiveBoardState) => {
		event.preventDefault()
		if (draggedTicket && draggedTicket.source !== destination) moveTicket(draggedTicket.id, destination)
		setDraggedTicket(null)
	}

	const allTickets = [...(snapshot?.activeTickets ?? []), ...(snapshot?.archivedTickets ?? [])]
	const selectedTicket = allTickets.find(({ id }) => id === selectedTicketId)

	return (
		<main className="relative flex h-full min-h-0 flex-col bg-vscode-editor-background" data-testid="board-view">
			<header className="border-b border-vscode-panel-border px-5 py-4">
				<div className="flex items-center justify-between gap-4">
					<h1 className="m-0 text-lg font-semibold text-vscode-foreground">Board</h1>
					{availableScopes.length > 0 && (
						<select
							aria-label="Board scope"
							className="min-w-0 max-w-64 rounded border border-vscode-dropdown-border bg-vscode-dropdown-background px-2 py-1 text-vscode-dropdown-foreground"
							value={state?.selectedBoardId ?? ""}
							onChange={(event) => {
								const scope = availableScopes.find(({ id }) => id === event.target.value)
								if (scope) vscode.postMessage({ type: "select_board_scope", scope } as never)
							}}>
							<option value="" disabled>
								Select a board
							</option>
							{availableScopes.map((scope) => (
								<option key={scope.id} value={scope.id}>
									{scope.rootPath}
								</option>
							))}
						</select>
					)}
				</div>
				<p className="m-0 mt-1 truncate text-sm text-vscode-descriptionForeground" title={cwd}>
					{cwd || "Open a repository to view its board"}
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
								onDragOver={(event) => {
									if (draggedTicket?.source !== column) event.preventDefault()
								}}
								onDrop={(event) => dropTicket(event, column)}
								className={`flex h-full min-h-0 flex-col rounded border border-vscode-panel-border bg-vscode-sideBar-background ${compact ? "w-full min-w-0" : "w-72 min-w-72"}`}>
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
										draggedTicket
											? "Drop to execute or resume this ticket."
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
									{snapshot.board.columns[column].map((ticketId) => {
										const ticket = ticketsById.get(ticketId)
										return ticket ? (
											<div
												key={ticketId}
												draggable
												onDragStart={(event) => {
													event.dataTransfer.effectAllowed = "move"
													event.dataTransfer.setData("text/plain", ticketId)
													setDraggedTicket({ id: ticketId, source: column })
												}}
												onDragEnd={() => setDraggedTicket(null)}
												className="min-w-0">
												<TicketCard
													ticket={ticket}
													column={column}
													onAction={performTicketAction}
													onOpen={setSelectedTicketId}
												/>
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
								<button
									key={ticket.id}
									className="ml-2 text-vscode-textLink-foreground hover:underline"
									onClick={() => setSelectedTicketId(ticket.id)}>
									{ticket.id}
								</button>
							))}
						</footer>
					)}
				</div>
			)}
			{selectedTicket && (
				<TicketDetailView
					ticket={selectedTicket}
					tickets={allTickets}
					onBack={() => setSelectedTicketId(null)}
					onOpenTicket={setSelectedTicketId}
				/>
			)}
		</main>
	)
}

export default BoardView
