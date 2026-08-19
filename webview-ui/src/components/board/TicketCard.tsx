import type { Ticket } from "@roo-code/types"

type ActiveBoardState = Exclude<Ticket["lifecycle"]["state"], "archived">

interface TicketCardProps {
	ticket: Ticket
	column: ActiveBoardState
	onAction: (operation: string, ticketId: string, destination?: ActiveBoardState) => void
}

const actionByState: Record<ActiveBoardState, { label: string; operation: string; destination?: ActiveBoardState }> = {
	backlog: { label: "Mark ready", operation: "move_ticket", destination: "ready" },
	ready: { label: "Execute", operation: "start_ticket_execution" },
	in_progress: { label: "Cancel execution", operation: "cancel_ticket_execution" },
	blocked: { label: "Resolve blocker", operation: "move_ticket", destination: "ready" },
	review: { label: "Accept", operation: "accept_ticket" },
	done: { label: "Archive", operation: "archive_ticket" },
}

const TicketCard = ({ ticket, column, onAction }: TicketCardProps) => {
	const unresolvedBlocker = [...ticket.lifecycle.blockedReasons].reverse().find(({ resolvedAt }) => !resolvedAt)
	const failedAttempts = ticket.lifecycle.failedAttempts.length
	const reviewCycles = ticket.lifecycle.reviewComments.length
	const resumable = column === "blocked" && ticket.execution.historyItemIds.length > 0
	const action = resumable ? { label: "Resume", operation: "start_ticket_execution" } : actionByState[column]

	return (
		<article
			aria-label={`${ticket.id}: ${ticket.statementOfWork.title}`}
			className="min-w-0 rounded border border-vscode-panel-border bg-vscode-editor-background p-3 shadow-sm">
			<div className="flex min-w-0 items-start justify-between gap-2">
				<span className="shrink-0 font-mono text-xs font-semibold text-vscode-descriptionForeground">
					{ticket.id}
				</span>
				<span className="truncate text-[11px] capitalize text-vscode-descriptionForeground">
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
					{ticket.statementOfWork.dependencies.length
						? `${ticket.statementOfWork.dependencies.length} dependencies`
						: "No dependencies"}
				</span>
				<button
					type="button"
					className="shrink-0 rounded bg-vscode-button-background px-2.5 py-1 text-xs font-medium text-vscode-button-foreground hover:bg-vscode-button-hoverBackground"
					onClick={() => onAction(action.operation, ticket.id, action.destination)}>
					{action.label}
				</button>
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
