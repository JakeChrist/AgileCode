import type { Ticket } from "@roo-code/types"

import { vscode } from "@/utils/vscode"

interface TicketDetailViewProps {
	ticket: Ticket
	tickets: Ticket[]
	onBack: () => void
	onOpenTicket: (ticketId: string) => void
}

const labels: Record<string, string> = {
	backlog: "Backlog",
	ready: "Ready",
	in_progress: "In progress",
	blocked: "Blocked",
	review: "Review",
	done: "Done",
	archived: "Archived",
}

const formatDate = (value?: string) => (value ? new Date(value).toLocaleString() : "Not recorded")

const TextList = ({ items }: { items?: string[] }) =>
	items?.length ? (
		<ul className="m-0 space-y-1 pl-5">
			{items.map((item, index) => (
				<li key={`${index}-${item}`}>{item}</li>
			))}
		</ul>
	) : (
		<p className="m-0 italic text-vscode-descriptionForeground">Not provided</p>
	)

const Section = ({ title, children }: { title: string; children: React.ReactNode }) => (
	<section className="border-t border-vscode-panel-border py-4">
		<h2 className="m-0 mb-2 text-sm font-semibold">{title}</h2>
		<div className="text-sm leading-6">{children}</div>
	</section>
)

const TicketDetailView = ({ ticket, tickets, onBack, onOpenTicket }: TicketDetailViewProps) => {
	const { statementOfWork: sow, lifecycle, execution } = ticket
	const ticketIds = new Set(tickets.map(({ id }) => id))
	const locked = !["backlog", "ready"].includes(lifecycle.state)

	return (
		<main
			aria-labelledby="ticket-detail-title"
			className="absolute inset-0 z-10 flex flex-col bg-vscode-editor-background"
			data-testid="ticket-detail-view">
			<header className="shrink-0 border-b border-vscode-panel-border px-4 py-3">
				<button
					data-ticket-detail-back
					ref={(element) => element?.focus()}
					type="button"
					className="mb-3 text-sm text-vscode-textLink-foreground hover:underline"
					onClick={onBack}>
					← Back to board
				</button>
				<div className="flex items-start justify-between gap-4">
					<div>
						<span className="font-mono text-xs text-vscode-descriptionForeground">{ticket.id}</span>
						<h1 id="ticket-detail-title" className="m-0 mt-1 text-xl font-semibold">
							{sow.title}
						</h1>
					</div>
					<span
						role="status"
						aria-label={`Current workflow state: ${labels[lifecycle.state]}`}
						className="rounded border border-vscode-panel-border px-2 py-1 text-xs">
						{labels[lifecycle.state]}
					</span>
				</div>
			</header>
			<div className="min-h-0 flex-1 overflow-y-auto px-5 pb-8">
				<div className="my-4 rounded border border-vscode-panel-border bg-vscode-sideBar-background p-3 text-sm">
					<strong>Read-only ticket</strong>
					<p className="m-0 mt-1 text-vscode-descriptionForeground">
						{locked
							? `Editing is unavailable while this ticket is ${labels[lifecycle.state].toLowerCase()}; its lifecycle state locks the statement of work.`
							: "This detail view is read-only. Return to the board to use available ticket actions."}
					</p>
				</div>

				<p className="mb-1 text-xs font-semibold uppercase tracking-wide text-vscode-descriptionForeground">
					Customer-authored statement of work
				</p>
				<Section title="Objective">
					<p className="m-0 whitespace-pre-wrap">{sow.objective}</p>
				</Section>
				<Section title="Context">
					<p className="m-0 whitespace-pre-wrap">{sow.context}</p>
				</Section>
				<Section title="Requirements">
					<TextList items={sow.requirements} />
				</Section>
				<Section title="Deliverables">
					<TextList items={sow.deliverables} />
				</Section>
				<Section title="Constraints">
					<TextList items={sow.constraints} />
				</Section>
				<Section title="Included scope">
					<TextList items={sow.includedScope} />
				</Section>
				<Section title="Excluded scope">
					<TextList items={sow.excludedScope} />
				</Section>
				<Section title="Dependencies">
					{sow.dependencies.length ? (
						<ul className="m-0 space-y-1 pl-5">
							{sow.dependencies.map((dependency) => (
								<li key={dependency}>
									{ticketIds.has(dependency) ? (
										<button
											className="text-vscode-textLink-foreground hover:underline"
											onClick={() => onOpenTicket(dependency)}>
											{dependency}
										</button>
									) : (
										<span>
											{dependency}{" "}
											<span className="text-vscode-descriptionForeground">
												(ticket unavailable)
											</span>
										</span>
									)}
								</li>
							))}
						</ul>
					) : (
						<p className="m-0 italic text-vscode-descriptionForeground">None</p>
					)}
				</Section>
				<Section title="Acceptance criteria">
					<TextList items={sow.acceptanceCriteria} />
				</Section>
				<Section title="Validation">
					<TextList items={sow.validation} />
				</Section>

				<p className="mb-1 mt-5 text-xs font-semibold uppercase tracking-wide text-vscode-descriptionForeground">
					System-managed lifecycle and execution
				</p>
				<Section title="Lifecycle timestamps">
					<dl className="m-0 grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-4">
						<dt>Created</dt>
						<dd className="m-0">{formatDate(lifecycle.createdAt)}</dd>
						<dt>Completed</dt>
						<dd className="m-0">{formatDate(lifecycle.completedAt)}</dd>
						<dt>Accepted</dt>
						<dd className="m-0">{formatDate(lifecycle.acceptedAt)}</dd>
						<dt>Archived</dt>
						<dd className="m-0">{formatDate(lifecycle.archivedAt)}</dd>
						<dt>Archived from</dt>
						<dd className="m-0">
							{lifecycle.archivedFrom ? labels[lifecycle.archivedFrom] : "Not recorded"}
						</dd>
					</dl>
				</Section>
				<Section title="Blocked reasons">
					{lifecycle.blockedReasons.length ? (
						lifecycle.blockedReasons.map((item, index) => (
							<div key={index} className="mb-2">
								<p className="m-0">{item.reason}</p>
								<small className="text-vscode-descriptionForeground">
									Recorded {formatDate(item.createdAt)} · Resolved {formatDate(item.resolvedAt)}
								</small>
							</div>
						))
					) : (
						<p className="m-0 italic text-vscode-descriptionForeground">None</p>
					)}
				</Section>
				<Section title="Review feedback">
					{lifecycle.reviewComments.length ? (
						lifecycle.reviewComments.map((item) => (
							<article key={item.id} className="mb-3">
								<p className="m-0 whitespace-pre-wrap">{item.comment}</p>
								<small className="text-vscode-descriptionForeground">
									{item.author ?? "Author not recorded"} · {formatDate(item.createdAt)}
								</small>
							</article>
						))
					) : (
						<p className="m-0 italic text-vscode-descriptionForeground">None</p>
					)}
				</Section>
				<Section title="Failed attempts">
					{lifecycle.failedAttempts.length ? (
						lifecycle.failedAttempts.map((attempt) => (
							<article key={attempt.historyItemId} className="mb-3">
								<p className="m-0">{attempt.summary}</p>
								<small className="text-vscode-descriptionForeground">
									Failed {formatDate(attempt.failedAt)} ·{" "}
								</small>
								<HistoryLink id={attempt.historyItemId} />
							</article>
						))
					) : (
						<p className="m-0 italic text-vscode-descriptionForeground">None</p>
					)}
				</Section>
				<Section title="Execution history">
					{execution.historyItemIds.length ? (
						<ul className="m-0 space-y-1 pl-5">
							{execution.historyItemIds.map((id) => (
								<li key={id}>
									<HistoryLink id={id} />
								</li>
							))}
						</ul>
					) : (
						<p className="m-0 italic text-vscode-descriptionForeground">No task history referenced</p>
					)}
				</Section>
			</div>
		</main>
	)
}

const HistoryLink = ({ id }: { id: string }) => (
	<button
		type="button"
		className="text-vscode-textLink-foreground hover:underline"
		onClick={() => vscode.postMessage({ type: "showTaskWithId", text: id })}>
		Open task {id}
	</button>
)

export default TicketDetailView
