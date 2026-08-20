import type { Ticket } from "@roo-code/types"

import { manualTicketTransitions } from "./ticketTransitions"

type ActiveBoardState = Exclude<Ticket["lifecycle"]["state"], "archived">

interface TicketCardProps {
	ticket: Ticket
	tickets: Ticket[]
	column: ActiveBoardState
	onAction: (operation: string, ticketId: string, destination?: ActiveBoardState, comment?: string) => void
	onOpen: (ticketId: string) => void
}

const actionByState: Record<ActiveBoardState, { label: string; operation: string; destination?: ActiveBoardState }> = {
	backlog: { label: "Mark ready", operation: "move_ticket", destination: "ready" },
	ready: { label: "Execute", operation: "start_ticket_execution" },
	in_progress: { label: "Cancel execution", operation: "cancel_ticket_execution" },
	blocked: { label: "Resolve blocker", operation: "move_ticket", destination: "ready" },
	review: { label: "Accept", operation: "accept_ticket" },
	done: { label: "Archive", operation: "archive_ticket" },
}

const TicketCard = ({ ticket, tickets, column, onAction, onOpen }: TicketCardProps) => {
	const unresolvedBlocker = [...ticket.lifecycle.blockedReasons].reverse().find(({ resolvedAt }) => !resolvedAt)
	const failedAttempts = ticket.lifecycle.failedAttempts.length
	const reviewCycles = ticket.lifecycle.reviewComments.length
	const resumable = column === "blocked" && ticket.execution.historyItemIds.length > 0
	const action = resumable ? { label: "Resume", operation: "start_ticket_execution" } : actionByState[column]
	const transitions = manualTicketTransitions(ticket)
	const dependencies = ticket.statementOfWork.dependencies.map((id) => {
		const prerequisite = tickets.find((candidate) => candidate.id === id)
		const state = prerequisite?.lifecycle.state
		const effectiveState = state === "archived" ? prerequisite?.lifecycle.archivedFrom : state
		return { id, state, resolved: effectiveState === "done" }
	})
	const unresolvedDependencies = dependencies.filter(({ resolved }) => !resolved)

	return (
		<article
			aria-label={`${ticket.id}: ${ticket.statementOfWork.title}`}
			aria-describedby={`${ticket.id}-state`}
			data-ticket-id={ticket.id}
			tabIndex={0}
			onKeyDown={(event) => {
				if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return
				event.preventDefault()
				onOpen(ticket.id)
			}}
			className="min-w-0 rounded border border-vscode-panel-border bg-vscode-editor-background p-3 shadow-sm">
			<div className="flex min-w-0 items-start justify-between gap-2">
				<span className="shrink-0 font-mono text-xs font-semibold text-vscode-descriptionForeground">
					{ticket.id}
				</span>
				<span
					id={`${ticket.id}-state`}
					className="truncate text-[11px] capitalize text-vscode-descriptionForeground">
					{column.replace("_", " ")}
				</span>
			</div>
			<h3 className="m-0 mt-1 line-clamp-2 break-words text-sm font-medium leading-5 text-vscode-foreground">
				{ticket.statementOfWork.title}
			</h3>
			<p className="m-0 mt-1 line-clamp-2 break-words text-xs leading-4 text-vscode-descriptionForeground">
				{ticket.statementOfWork.objective}
			</p>

			<div className="mt-2 flex flex-wrap gap-1" aria-label={`${ticket.id} conditions`}>
				{unresolvedDependencies.length > 0 && (
					<Condition tone="blocked">
						Waiting for {unresolvedDependencies.map(({ id }) => id).join(", ")}
					</Condition>
				)}
				{column === "in_progress" && <Condition tone="active">Execution active</Condition>}
				{column === "blocked" && unresolvedBlocker && (
					<Condition tone={/waiting for user/i.test(unresolvedBlocker.reason) ? "waiting" : "blocked"}>
						{unresolvedBlocker.reason}
					</Condition>
				)}
				{column === "blocked" && resumable && <Condition tone="active">Resumable</Condition>}
				{column === "review" && <Condition tone="review">Feedback outstanding</Condition>}
				{reviewCycles > 0 && (
					<Condition tone="review">
						{reviewCycles} prior rejection {reviewCycles === 1 ? "cycle" : "cycles"}
					</Condition>
				)}
				{failedAttempts > 0 && (
					<Condition tone="failed">
						{failedAttempts} failed {failedAttempts === 1 ? "attempt" : "attempts"}
					</Condition>
				)}
				{column === "done" && <Condition tone="done">Accepted</Condition>}
			</div>

			<div className="mt-3 flex items-center justify-between gap-2">
				<span className="min-w-0 truncate text-[11px] text-vscode-descriptionForeground">
					{dependencies.length
						? `${dependencies.length} dependencies · ${dependencies.length - unresolvedDependencies.length} complete`
						: "No dependencies"}
				</span>
				<div className="flex shrink-0 gap-1">
					{transitions.length > 0 && (
						<label>
							<span className="sr-only">Change status for {ticket.id}</span>
							<select
								aria-label={`Change status for ${ticket.id}`}
								className="max-w-28 rounded border border-vscode-dropdown-border bg-vscode-dropdown-background px-1 py-1 text-xs text-vscode-dropdown-foreground"
								value=""
								onChange={(event) => {
									const transition = transitions.find(
										({ destination }) => destination === event.target.value,
									)
									if (transition)
										onAction(
											transition.operation,
											ticket.id,
											transition.operation === "move_ticket" ? transition.destination : undefined,
										)
								}}>
								<option value="">Status actions…</option>
								{transitions.map((transition) => (
									<option key={transition.destination} value={transition.destination}>
										{transition.label}
									</option>
								))}
							</select>
						</label>
					)}
					<button
						type="button"
						className="rounded border border-vscode-button-border px-2.5 py-1 text-xs text-vscode-foreground hover:bg-vscode-list-hoverBackground"
						aria-label={`Open ${ticket.id}: ${ticket.statementOfWork.title}`}
						onClick={() => onOpen(ticket.id)}>
						Details
					</button>
					<button
						type="button"
						className="shrink-0 rounded bg-vscode-button-background px-2.5 py-1 text-xs font-medium text-vscode-button-foreground hover:bg-vscode-button-hoverBackground"
						aria-label={`${action.label} ${ticket.id}`}
						disabled={action.operation === "start_ticket_execution" && unresolvedDependencies.length > 0}
						onClick={() => onAction(action.operation, ticket.id, action.destination)}>
						{action.label}
					</button>
					{column === "review" && (
						<button
							type="button"
							className="rounded border border-vscode-button-border px-2.5 py-1 text-xs text-vscode-foreground hover:bg-vscode-list-hoverBackground"
							aria-label={`Reject ${ticket.id}`}
							onClick={() => {
								const comment = window.prompt(`Feedback for ${ticket.id}`)
								if (comment?.trim()) onAction("reject_ticket", ticket.id, undefined, comment.trim())
							}}>
							Reject
						</button>
					)}
				</div>
			</div>
		</article>
	)
}

const conditionClasses = {
	active: "border-vscode-charts-blue text-vscode-charts-blue",
	waiting: "border-vscode-charts-yellow text-vscode-charts-yellow",
	blocked: "border-vscode-charts-orange text-vscode-charts-orange",
	review: "border-vscode-charts-purple text-vscode-charts-purple",
	failed: "border-vscode-errorForeground text-vscode-errorForeground",
	done: "border-vscode-charts-green text-vscode-charts-green",
} as const

const Condition = ({ children, tone }: { children: React.ReactNode; tone: keyof typeof conditionClasses }) => (
	<span
		className={`max-w-full truncate rounded-full border px-2 py-0.5 text-[11px] ${conditionClasses[tone]}`}
		title={String(children)}>
		{children}
	</span>
)

export default TicketCard
