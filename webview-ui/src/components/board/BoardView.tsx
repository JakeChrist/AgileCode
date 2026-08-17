import { useEffect, useState } from "react"

import { activeBoardStates, type TicketWorkflowState } from "@roo-code/types"

import { useBoardState } from "@/context/BoardStateContext"
import { useExtensionState } from "@/context/ExtensionStateContext"
import { vscode } from "@/utils/vscode"

const columnLabels: Record<(typeof activeBoardStates)[number], string> = {
	backlog: "Backlog",
	ready: "Ready",
	in_progress: "In progress",
	blocked: "Blocked",
	review: "Review",
	done: "Done",
}

const compactBoardQuery = "(max-width: 600px)"

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
	const [selectedColumn, setSelectedColumn] = useState<TicketWorkflowState>(activeBoardStates[0])
	const visibleColumns = compact ? [selectedColumn] : activeBoardStates

	const moveTicket = (ticketId: string, destination: TicketWorkflowState) => {
		if (!selectedBoard || destination === "archived") return
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

	return (
		<main className="flex h-full min-h-0 flex-col bg-vscode-editor-background" data-testid="board-view">
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
			{status === "error" && (
				<div className="m-auto max-w-lg px-6 text-center text-vscode-errorForeground">
					{selectedBoard?.error?.message ?? "The board could not be loaded."}
				</div>
			)}
			{status === "ready" && snapshot && (
				<div className="flex min-h-0 flex-1 flex-col">
					{compact && (
						<label className="shrink-0 px-4 pt-3 text-xs font-medium text-vscode-descriptionForeground">
							Workflow column
							<select
								aria-label="Workflow column"
								className="mt-1 w-full rounded border border-vscode-dropdown-border bg-vscode-dropdown-background px-2 py-2 text-sm text-vscode-dropdown-foreground"
								value={selectedColumn}
								onChange={(event) => setSelectedColumn(event.target.value as TicketWorkflowState)}>
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
								className={`flex h-full min-h-0 flex-col rounded border border-vscode-panel-border bg-vscode-sideBar-background ${compact ? "w-full min-w-0" : "w-72 min-w-72"}`}>
								<h2 className="m-0 shrink-0 border-b border-vscode-panel-border px-3 py-3 text-sm font-semibold text-vscode-foreground">
									{columnLabels[column]}{" "}
									<span className="text-vscode-descriptionForeground">
										{snapshot.board.columns[column].length}
									</span>
								</h2>
								<div
									className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3"
									aria-label={`${columnLabels[column]} tickets`}>
									{snapshot.board.columns[column].map((ticketId) => {
										const ticket = ticketsById.get(ticketId)
										return (
											<article
												key={ticketId}
												className="rounded border border-vscode-panel-border bg-vscode-editor-background p-3">
												<div className="text-xs text-vscode-descriptionForeground">
													{ticketId}
												</div>
												<div className="mt-1 text-sm text-vscode-foreground">
													{ticket?.statementOfWork.title ?? ticketId}
												</div>
												<label className="mt-3 block text-xs text-vscode-descriptionForeground">
													Move to
													<select
														aria-label={`Move ${ticketId} to`}
														className="mt-1 w-full rounded border border-vscode-dropdown-border bg-vscode-dropdown-background px-2 py-1 text-vscode-dropdown-foreground"
														value={column}
														onChange={(event) =>
															moveTicket(
																ticketId,
																event.target.value as TicketWorkflowState,
															)
														}>
														{activeBoardStates.map((destination) => (
															<option key={destination} value={destination}>
																{columnLabels[destination]}
															</option>
														))}
													</select>
												</label>
											</article>
										)
									})}
								</div>
							</section>
						))}
					</div>
				</div>
			)}
		</main>
	)
}

export default BoardView
