import { activeBoardStates } from "@roo-code/types"

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

const BoardView = () => {
	const { cwd } = useExtensionState()
	const { selectedBoard, status, state } = useBoardState()
	const availableScopes = state?.availableScopes ?? []
	const snapshot = selectedBoard?.snapshot
	const ticketsById = new Map(snapshot?.activeTickets.map((ticket) => [ticket.id, ticket]))

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
				<div className="flex min-h-0 flex-1 gap-3 overflow-x-auto p-4" aria-label="Kanban board">
					{activeBoardStates.map((column) => (
						<section
							key={column}
							className="w-64 min-w-64 overflow-y-auto rounded border border-vscode-panel-border bg-vscode-sideBar-background p-3">
							<h2 className="m-0 mb-3 text-sm font-semibold text-vscode-foreground">
								{columnLabels[column]}{" "}
								<span className="text-vscode-descriptionForeground">
									{snapshot.board.columns[column].length}
								</span>
							</h2>
							<div className="flex flex-col gap-2">
								{snapshot.board.columns[column].map((ticketId) => {
									const ticket = ticketsById.get(ticketId)
									return (
										<article
											key={ticketId}
											className="rounded border border-vscode-panel-border bg-vscode-editor-background p-3">
											<div className="text-xs text-vscode-descriptionForeground">{ticketId}</div>
											<div className="mt-1 text-sm text-vscode-foreground">
												{ticket?.statementOfWork.title ?? ticketId}
											</div>
										</article>
									)
								})}
							</div>
						</section>
					))}
				</div>
			)}
		</main>
	)
}

export default BoardView
