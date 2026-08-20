import userEvent from "@testing-library/user-event"

import { fireEvent, render, screen } from "@/utils/test-utils"
import { vscode } from "@/utils/vscode"

import BoardView from "../BoardView"

const mockUseBoardState = vi.fn()
const mockUseExtensionState = vi.fn()
const dispatch = vi.fn()

vi.mock("@/context/BoardStateContext", () => ({ useBoardState: () => mockUseBoardState() }))
vi.mock("@/context/ExtensionStateContext", () => ({ useExtensionState: () => mockUseExtensionState() }))

const columns = (overrides: Partial<Record<string, string[]>> = {}) => ({
	backlog: [],
	ready: [],
	in_progress: [],
	blocked: [],
	review: [],
	done: [],
	...overrides,
})

const showBoard = (columnOverrides: Partial<Record<string, string[]>> = {}, activeTickets: object[] = []) => {
	const completeTickets = activeTickets.map((value) => {
		const ticket = value as Record<string, any>
		return {
			formatVersion: 1,
			...ticket,
			statementOfWork: {
				objective: "A concise objective",
				context: "Context",
				requirements: ["Requirement that must stay off the card"],
				constraints: ["Constraint"],
				includedScope: ["Scope"],
				dependencies: [],
				acceptanceCriteria: ["Criterion"],
				validation: ["Validation"],
				...ticket.statementOfWork,
			},
			lifecycle: {
				state: "backlog",
				createdAt: "2026-08-14T00:00:00.000Z",
				reviewComments: [],
				blockedReasons: [],
				failedAttempts: [],
				...ticket.lifecycle,
			},
			execution: { historyItemIds: [], ...ticket.execution },
		}
	})
	mockUseExtensionState.mockReturnValue({ cwd: "/workspace/agile-code" })
	mockUseBoardState.mockReturnValue({
		dispatch,
		state: { availableScopes: [], selectedBoardId: "git:board" },
		status: "ready",
		selectedBoard: {
			scope: { id: "git:board", kind: "git", rootPath: "/workspace/agile-code" },
			snapshot: {
				board: { columns: columns(columnOverrides), archiveOrder: ["AC-ARCHIVED"] },
				activeTickets: completeTickets,
				archivedTickets: [
					{
						formatVersion: 1,
						id: "AC-ARCHIVED",
						statementOfWork: {
							title: "Archived ticket",
							objective: "Old objective",
							context: "Old context",
							requirements: ["Old requirement"],
							constraints: ["Old constraint"],
							includedScope: ["Old scope"],
							dependencies: [],
							acceptanceCriteria: ["Was accepted"],
							validation: ["Was validated"],
						},
						lifecycle: {
							state: "archived",
							createdAt: "2026-08-01T00:00:00.000Z",
							archivedAt: "2026-08-14T00:00:00.000Z",
							archivedFrom: "done",
							reviewComments: [],
							blockedReasons: [],
							failedAttempts: [],
						},
						execution: { historyItemIds: [] },
					},
				],
			},
		},
	})
}

describe("BoardView", () => {
	afterEach(() => {
		vi.unstubAllGlobals()
		dispatch.mockReset()
	})

	it("labels same-named Git and non-Git scopes distinctly and switches authoritatively", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard()
		const git = { id: "git:other", kind: "git" as const, rootPath: "/clients/two/app" }
		mockUseBoardState.mockReturnValue({
			...mockUseBoardState(),
			dispatch,
			state: {
				selectedBoardId: "git:board",
				availableScopes: [
					{ id: "git:board", kind: "git", rootPath: "/clients/one/app" },
					git,
					{ id: "workspace:docs", kind: "workspace", rootPath: "/clients/docs" },
				],
			},
		})
		render(<BoardView />)

		const selector = screen.getByRole("combobox", { name: "Board scope" })
		expect(selector).toHaveTextContent("app — clients/one (Git repository)")
		expect(selector).toHaveTextContent("app — clients/two (Git repository)")
		expect(selector).toHaveTextContent("docs — clients (Workspace folder (non-Git))")
		await userEvent.selectOptions(selector, git.id)

		expect(dispatch).toHaveBeenCalledWith({ type: "select", scope: git })
		expect(postMessage).toHaveBeenCalledWith({ type: "select_board_scope", scope: git })
	})

	it("keeps a dirty ticket draft and cancels the scope switch unless explicitly resolved", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
		showBoard()
		const other = { id: "git:other", kind: "git" as const, rootPath: "/clients/other" }
		mockUseBoardState.mockReturnValue({
			...mockUseBoardState(),
			dispatch,
			selectedBoard: { ...mockUseBoardState().selectedBoard, draft: { dirty: true, values: { title: "Draft" } } },
			state: { selectedBoardId: "git:board", availableScopes: [other] },
		})
		render(<BoardView />)
		await userEvent.selectOptions(screen.getByRole("combobox", { name: "Board scope" }), other.id)

		expect(confirm).toHaveBeenCalled()
		expect(dispatch).not.toHaveBeenCalled()
		expect(postMessage).not.toHaveBeenCalledWith(expect.objectContaining({ type: "select_board_scope" }))
	})

	it("opens the selected board in the full editor context", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard()
		render(<BoardView />)

		await userEvent.click(screen.getByRole("button", { name: "Open Full Board" }))
		expect(postMessage).toHaveBeenCalledWith({ type: "open_full_board", boardId: "git:board" })
	})

	it.each([
		["loading", "Loading board…"],
		["uninitialized", "No ticket store exists yet."],
	])("renders the %s state without stale ticket content", (status, message) => {
		mockUseExtensionState.mockReturnValue({ cwd: "/workspace/agile-code" })
		mockUseBoardState.mockReturnValue({
			status,
			state: { availableScopes: [] },
			selectedBoard: { scope: { id: "git:board", rootPath: "/workspace/agile-code" } },
		})
		render(<BoardView />)
		expect(screen.getByText(message, { exact: false })).toBeInTheDocument()
		expect(screen.queryByLabelText("Kanban board")).not.toBeInTheDocument()
	})

	it("retries a fatal failure with a new authoritative load request", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		mockUseExtensionState.mockReturnValue({ cwd: "/workspace/agile-code" })
		mockUseBoardState.mockReturnValue({
			status: "error",
			state: { availableScopes: [] },
			selectedBoard: {
				scope: { id: "git:board", rootPath: "/workspace/agile-code" },
				error: { message: "Cannot read store", retryable: true },
				diagnostics: ["store.json: permission denied"],
			},
		})
		render(<BoardView />)
		await userEvent.click(screen.getByRole("button", { name: "Retry" }))
		expect(postMessage).toHaveBeenCalledWith(
			expect.objectContaining({ request: expect.objectContaining({ operation: "load_board" }) }),
		)
	})

	it("renders the selected repository and its shared board snapshot", () => {
		showBoard({ backlog: ["AC-014"] }, [{ id: "AC-014", statementOfWork: { title: "Add Board navigation" } }])

		render(<BoardView />)

		expect(screen.getByTitle("/workspace/agile-code")).toHaveTextContent("agile-code — workspace (Git repository)")
		expect(screen.getByText("AC-014")).toBeInTheDocument()
		expect(screen.getByText("Add Board navigation")).toBeInTheDocument()
		expect(screen.getByLabelText("Kanban board")).toBeInTheDocument()
	})

	it("renders all active columns in workflow order without an archive column", () => {
		showBoard()
		render(<BoardView />)

		expect(screen.getAllByRole("region").map((column) => column.getAttribute("aria-label"))).toEqual([
			"Backlog column",
			"Ready column",
			"In Progress column",
			"Blocked column",
			"Review column",
			"Done column",
		])
		expect(screen.queryByText("Archived ticket")).not.toBeInTheDocument()
		expect(screen.queryByLabelText("Archived column")).not.toBeInTheDocument()
	})

	it("shows authoritative counts and meaningful empty-state guidance", () => {
		showBoard({ ready: ["AC-020"] }, [{ id: "AC-020", statementOfWork: { title: "Column guidance" } }])
		render(<BoardView />)

		expect(screen.getByLabelText("Ready column")).toHaveTextContent("Ready 1")
		expect(screen.getByLabelText("Backlog column")).toHaveTextContent("Backlog 0")
		expect(screen.getByText("No tickets have been prioritized yet.")).toBeInTheDocument()
		expect(screen.getByText("No tickets are awaiting your acceptance or rejection.")).toBeInTheDocument()
	})

	it("explains Review and distinguishes both kinds of blocked ticket", () => {
		showBoard({ blocked: ["AC-001", "AC-002"] }, [
			{
				id: "AC-001",
				statementOfWork: { title: "Manual blocker" },
				lifecycle: {
					state: "blocked",
					blockedReasons: [{ reason: "Manual block", createdAt: "2026-08-14T00:00:00.000Z" }],
				},
			},
			{
				id: "AC-002",
				statementOfWork: { title: "Paused work" },
				lifecycle: {
					state: "blocked",
					blockedReasons: [{ reason: "Waiting for User", createdAt: "2026-08-14T00:00:00.000Z" }],
				},
				execution: { historyItemIds: ["history-1"] },
			},
		])
		render(<BoardView />)

		expect(screen.getByText("Tickets here await your acceptance or rejection.")).toBeInTheDocument()
		expect(screen.getByText("Manual block")).toBeInTheDocument()
		expect(screen.getByLabelText("AC-002 conditions")).toHaveTextContent("Waiting for UserResumable")
	})

	it("shows execution guidance only while an eligible ticket is dragged", () => {
		showBoard({ ready: ["AC-020"] }, [
			{ id: "AC-020", statementOfWork: { title: "Execute me" }, lifecycle: { state: "ready" } },
		])
		render(<BoardView />)

		expect(screen.getByLabelText("In Progress column")).toHaveTextContent("Tickets currently executing.")
		const card = screen.getByText("AC-020").closest("[draggable]")!
		fireEvent.dragStart(card, {
			dataTransfer: { effectAllowed: "none", setData: vi.fn() },
		})
		expect(screen.getByLabelText("In Progress column")).toHaveTextContent("Drop to execute or resume this ticket.")
		fireEvent.dragEnd(card)
		expect(screen.getByLabelText("In Progress column")).not.toHaveTextContent(
			"Drop to execute or resume this ticket.",
		)
	})

	it("preserves authoritative ticket order within every populated column", () => {
		const orderedIds = ["AC-003", "AC-001", "AC-002"]
		showBoard(
			{ review: orderedIds },
			orderedIds.map((id) => ({ id, statementOfWork: { title: `Ticket ${id}` } })),
		)
		render(<BoardView />)

		const reviewList = screen.getByLabelText("Review tickets")
		expect(
			Array.from(reviewList.querySelectorAll("article")).map((card) => card.getAttribute("aria-label")),
		).toEqual(["AC-003: Ticket AC-003", "AC-001: Ticket AC-001", "AC-002: Ticket AC-002"])
	})

	it("uses one reorder request for keyboard controls and same-column dragging", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard({ backlog: ["AC-001", "AC-002", "AC-003"] }, [
			{ id: "AC-001", statementOfWork: { title: "First" } },
			{ id: "AC-002", statementOfWork: { title: "Second" } },
			{ id: "AC-003", statementOfWork: { title: "Third" } },
		])
		render(<BoardView />)

		await userEvent.click(screen.getByRole("button", { name: "Move AC-002 up" }))
		expect(postMessage).toHaveBeenLastCalledWith(
			expect.objectContaining({
				request: expect.objectContaining({
					operation: "reorder_tickets",
					state: "backlog",
					expectedOrder: ["AC-001", "AC-002", "AC-003"],
					orderedIds: ["AC-002", "AC-001", "AC-003"],
				}),
			}),
		)

		const dragged = screen.getByText("AC-003").closest("[draggable]")!
		const target = screen.getByText("AC-001").closest("[draggable]")!
		fireEvent.dragStart(dragged, { dataTransfer: { effectAllowed: "none", setData: vi.fn() } })
		fireEvent.drop(target)
		expect(postMessage).toHaveBeenLastCalledWith(
			expect.objectContaining({
				request: expect.objectContaining({
					operation: "reorder_tickets",
					expectedOrder: ["AC-001", "AC-002", "AC-003"],
					orderedIds: ["AC-003", "AC-001", "AC-002"],
				}),
			}),
		)
	})

	it("uses the authoritative transition operation for an ordinary cross-column drop", () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard({ backlog: ["AC-037"] }, [
			{ id: "AC-037", statementOfWork: { title: "Drag transitions" }, lifecycle: { state: "backlog" } },
		])
		render(<BoardView />)

		const card = screen.getByText("AC-037").closest("[draggable]")!
		fireEvent.dragStart(card, { dataTransfer: { effectAllowed: "none", setData: vi.fn() } })
		fireEvent.drop(screen.getByLabelText("Ready column"))

		expect(postMessage).toHaveBeenCalledWith(
			expect.objectContaining({
				request: expect.objectContaining({
					operation: "move_ticket",
					ticketId: "AC-037",
					destination: "ready",
					position: 0,
				}),
			}),
		)
	})

	it("marks invalid destinations unavailable and does not request a mutation when dropped", () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard({ backlog: ["AC-037"] }, [
			{ id: "AC-037", statementOfWork: { title: "Invalid drop" }, lifecycle: { state: "backlog" } },
		])
		render(<BoardView />)

		fireEvent.dragStart(screen.getByText("AC-037").closest("[draggable]")!, {
			dataTransfer: { effectAllowed: "none", setData: vi.fn() },
		})
		const done = screen.getByLabelText("Done column")
		expect(done).toHaveAttribute("aria-disabled", "true")
		expect(done).toHaveTextContent("This ticket cannot be dropped here.")
		fireEvent.drop(done)

		expect(postMessage).not.toHaveBeenCalledWith(expect.objectContaining({ type: "board_request" }))
		expect(screen.getByText("AC-037")).toBeInTheDocument()
	})

	it("requests execution without moving Ready ticket metadata on an In Progress drop", () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard({ ready: ["AC-037"] }, [
			{ id: "AC-037", statementOfWork: { title: "Execution drop" }, lifecycle: { state: "ready" } },
		])
		render(<BoardView />)

		fireEvent.dragStart(screen.getByText("AC-037").closest("[draggable]")!, {
			dataTransfer: { effectAllowed: "none", setData: vi.fn() },
		})
		fireEvent.drop(screen.getByLabelText("In Progress column"))

		expect(postMessage).toHaveBeenCalledWith(
			expect.objectContaining({
				request: expect.objectContaining({ operation: "start_ticket_execution", ticketId: "AC-037" }),
			}),
		)
		expect(postMessage).not.toHaveBeenCalledWith(
			expect.objectContaining({ request: expect.objectContaining({ operation: "move_ticket" }) }),
		)
		expect(screen.getByLabelText("Ready tickets")).toHaveTextContent("AC-037")
	})

	it("cancels a drag without issuing a request or changing the authoritative order", () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard({ backlog: ["AC-001", "AC-037"] }, [
			{ id: "AC-001", statementOfWork: { title: "First" } },
			{ id: "AC-037", statementOfWork: { title: "Cancelled drag" } },
		])
		render(<BoardView />)

		const card = screen.getByText("AC-037").closest("[draggable]")!
		fireEvent.dragStart(card, { dataTransfer: { effectAllowed: "none", setData: vi.fn() } })
		fireEvent.dragEnd(card)

		expect(postMessage).not.toHaveBeenCalledWith(expect.objectContaining({ type: "board_request" }))
		expect(
			Array.from(screen.getByLabelText("Backlog tickets").querySelectorAll("article")).map((item) =>
				item.getAttribute("aria-label"),
			),
		).toEqual(["AC-001: First", "AC-037: Cancelled drag"])
	})

	it("keeps high-volume ticket lists independently scrollable beneath their headers", () => {
		const ids = Array.from({ length: 100 }, (_, index) => `AC-${String(index + 1).padStart(3, "0")}`)
		showBoard(
			{ in_progress: ids },
			ids.map((id) => ({ id, statementOfWork: { title: id } })),
		)
		render(<BoardView />)

		const board = screen.getByLabelText("Kanban board")
		const column = screen.getByLabelText("In Progress column")
		const ticketList = screen.getByLabelText("In Progress tickets")
		expect(board).toHaveClass("overflow-x-auto", "overflow-y-hidden")
		expect(column).toHaveClass("min-w-72", "flex-col")
		expect(column.querySelector("h2")).toHaveClass("shrink-0")
		expect(ticketList).toHaveClass("min-h-0", "overflow-y-auto")
		expect(ticketList.querySelectorAll("article")).toHaveLength(100)
	})

	it("shows one readable column at sidebar width while every workflow state remains selectable", async () => {
		vi.stubGlobal("matchMedia", () => ({
			matches: true,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		}))
		showBoard({ backlog: ["AC-001"], review: ["AC-019"] }, [
			{ id: "AC-001", statementOfWork: { title: "First ticket" } },
			{ id: "AC-019", statementOfWork: { title: "Compact sidebar board" } },
		])
		render(<BoardView />)

		const selector = screen.getByLabelText("Workflow column")
		expect(Array.from(selector.querySelectorAll("option")).map((option) => option.textContent)).toEqual([
			"Backlog (1)",
			"Ready (0)",
			"In Progress (0)",
			"Blocked (0)",
			"Review (1)",
			"Done (0)",
		])
		expect(screen.getAllByRole("region")).toHaveLength(1)

		await userEvent.selectOptions(selector, "review")
		expect(screen.getByLabelText("Review column")).toHaveTextContent("Compact sidebar board")
		expect(screen.getAllByRole("region")).toHaveLength(1)
	})

	it("offers the principal action on compact ticket cards", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		vi.stubGlobal("matchMedia", () => ({
			matches: true,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		}))
		showBoard({ ready: ["AC-019"] }, [
			{ id: "AC-019", statementOfWork: { title: "Compact sidebar board" }, lifecycle: { state: "ready" } },
		])
		render(<BoardView />)

		await userEvent.selectOptions(screen.getByLabelText("Workflow column"), "ready")
		await userEvent.click(screen.getByRole("button", { name: "Execute AC-019" }))
		expect(postMessage).toHaveBeenCalledWith(
			expect.objectContaining({
				type: "board_request",
				request: expect.objectContaining({
					boardId: "git:board",
					operation: "start_ticket_execution",
					ticketId: "AC-019",
				}),
			}),
		)
	})

	it("opens cards with the keyboard and returns to the preserved board", async () => {
		showBoard({ ready: ["AC-024"] }, [
			{ id: "AC-024", statementOfWork: { title: "Keyboard navigation" }, lifecycle: { state: "ready" } },
		])
		render(<BoardView />)
		const card = screen.getByRole("article", { name: "AC-024: Keyboard navigation" })

		card.focus()
		fireEvent.keyDown(card, { key: "Enter" })
		expect(screen.getByTestId("ticket-detail-view")).toBeInTheDocument()
		await userEvent.click(screen.getByRole("button", { name: "← Back to board" }))
		expect(screen.getByRole("article", { name: "AC-024: Keyboard navigation" })).toBe(card)
	})

	it("edits an eligible ticket through the correlated update request", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard({ ready: ["AC-032"] }, [
			{ id: "AC-032", statementOfWork: { title: "Original title" }, lifecycle: { state: "ready" } },
		])
		render(<BoardView />)

		await userEvent.click(screen.getByRole("button", { name: "Open AC-032: Original title" }))
		await userEvent.click(screen.getByRole("button", { name: "Edit statement of work" }))
		fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Revised title" } })
		await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

		expect(postMessage).toHaveBeenCalledWith(
			expect.objectContaining({
				type: "board_request",
				request: expect.objectContaining({
					operation: "update_ticket",
					ticketId: "AC-032",
					statementOfWork: expect.objectContaining({ title: "Revised title" }),
				}),
			}),
		)
	})

	it.each([
		[
			"in_progress",
			[],
			"Editing is unavailable while this ticket is in progress because its approved statement of work is the contract for the running task.",
		],
		[
			"blocked",
			["task-1"],
			"Editing is unavailable while this ticket is blocked with execution context that can be resumed.",
		],
	] as const)("explains the execution lock for %s tickets", async (state, historyItemIds, explanation) => {
		showBoard({ [state]: ["AC-033"] }, [
			{
				id: "AC-033",
				statementOfWork: { title: "Protected scope" },
				lifecycle: { state },
				execution: { historyItemIds: [...historyItemIds] },
			},
		])
		render(<BoardView />)

		await userEvent.click(screen.getByRole("button", { name: "Open AC-033: Protected scope" }))
		expect(screen.getByTestId("ticket-detail-view")).toHaveTextContent(explanation)
		expect(screen.queryByRole("button", { name: "Edit statement of work" })).not.toBeInTheDocument()
	})

	it("keeps a pre-execution Blocked ticket editable", async () => {
		showBoard({ blocked: ["AC-033"] }, [
			{ id: "AC-033", statementOfWork: { title: "Blocked before execution" }, lifecycle: { state: "blocked" } },
		])
		render(<BoardView />)

		await userEvent.click(screen.getByRole("button", { name: "Open AC-033: Blocked before execution" }))
		expect(screen.getByRole("button", { name: "Edit statement of work" })).toBeInTheDocument()
	})

	it("exposes contextual action names, review alternatives, and archive controls", () => {
		showBoard({ review: ["AC-024"] }, [
			{ id: "AC-024", statementOfWork: { title: "Keyboard navigation" }, lifecycle: { state: "review" } },
		])
		render(<BoardView />)

		expect(screen.getByRole("button", { name: "Open AC-024: Keyboard navigation" })).toBeInTheDocument()
		expect(screen.getByRole("button", { name: "Accept AC-024" })).toBeInTheDocument()
		expect(screen.getByRole("button", { name: "Reject AC-024" })).toBeInTheDocument()
		expect(screen.getByRole("button", { name: "Open archived ticket AC-ARCHIVED" })).toBeInTheDocument()
		expect(screen.getByRole("button", { name: "Restore AC-ARCHIVED" })).toBeInTheDocument()
	})

	it.each([
		["backlog", ["Move to ready", "Move to blocked"]],
		["ready", ["Move to backlog", "Execute", "Move to blocked"]],
		["blocked", ["Move to backlog", "Move to ready"]],
	] as const)("offers only permitted explicit transitions from %s", (state, expected) => {
		showBoard({ [state]: ["AC-025"] }, [
			{ id: "AC-025", statementOfWork: { title: "Status controls" }, lifecycle: { state } },
		])
		render(<BoardView />)

		const control = screen.getByRole("combobox", { name: "Change status for AC-025" })
		expect(
			Array.from(control.querySelectorAll("option"))
				.slice(1)
				.map(({ textContent }) => textContent),
		).toEqual(expected)
	})

	it("starts execution from the keyboard status control without first moving metadata", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		showBoard({ ready: ["AC-025"] }, [
			{ id: "AC-025", statementOfWork: { title: "Status controls" }, lifecycle: { state: "ready" } },
		])
		render(<BoardView />)

		await userEvent.selectOptions(screen.getByRole("combobox", { name: "Change status for AC-025" }), "in_progress")
		expect(postMessage).toHaveBeenCalledWith(
			expect.objectContaining({
				type: "board_request",
				request: expect.objectContaining({ operation: "start_ticket_execution", ticketId: "AC-025" }),
			}),
		)
		expect(postMessage).not.toHaveBeenCalledWith(
			expect.objectContaining({
				request: expect.objectContaining({ operation: "move_ticket", destination: "in_progress" }),
			}),
		)
	})

	it("offers resume rather than metadata movement for a resumable blocked ticket", () => {
		showBoard({ blocked: ["AC-025"] }, [
			{
				id: "AC-025",
				statementOfWork: { title: "Status controls" },
				lifecycle: { state: "blocked" },
				execution: { historyItemIds: ["task-1"] },
			},
		])
		render(<BoardView />)

		const control = screen.getByRole("combobox", { name: "Change status for AC-025" })
		expect(control).toHaveTextContent("Resume")
		expect(control).not.toHaveTextContent("Move to ready")
	})

	it.each([
		["ready", "Execute"],
		["in_progress", "Execution active"],
		["review", "Feedback outstanding"],
		["done", "Accepted"],
	] as const)("renders the %s card condition and action", (state, expected) => {
		showBoard({ [state]: ["AC-021"] }, [
			{ id: "AC-021", statementOfWork: { title: "Concise cards" }, lifecycle: { state } },
		])
		render(<BoardView />)

		expect(screen.getByLabelText("AC-021: Concise cards")).toHaveTextContent(expected)
		expect(screen.queryByText("Requirement that must stay off the card")).not.toBeInTheDocument()
	})

	it("distinguishes waiting, manual blocking, failed attempts, and rejection cycles", () => {
		showBoard({ blocked: ["AC-001", "AC-002"], ready: ["AC-003"] }, [
			{
				id: "AC-001",
				statementOfWork: { title: "Needs an answer" },
				lifecycle: {
					state: "blocked",
					blockedReasons: [{ reason: "Waiting for User: choose API", createdAt: "2026-08-14T00:00:00.000Z" }],
				},
				execution: { historyItemIds: ["history-1"] },
			},
			{
				id: "AC-002",
				statementOfWork: { title: "Missing dependency" },
				lifecycle: {
					state: "blocked",
					blockedReasons: [{ reason: "Dependency AC-000", createdAt: "2026-08-14T00:00:00.000Z" }],
				},
			},
			{
				id: "AC-003",
				statementOfWork: { title: "Ready again" },
				lifecycle: {
					state: "ready",
					reviewComments: [{ id: "review-1", comment: "Revise", createdAt: "2026-08-14T00:00:00.000Z" }],
					failedAttempts: [
						{ historyItemId: "history-2", summary: "Tests failed", failedAt: "2026-08-14T00:00:00.000Z" },
					],
				},
			},
		])
		render(<BoardView />)

		expect(screen.getByLabelText("AC-001 conditions")).toHaveTextContent("Waiting for User: choose API")
		expect(screen.getByLabelText("AC-001: Needs an answer")).toHaveTextContent("Resume")
		expect(screen.getByLabelText("AC-002 conditions")).toHaveTextContent("Dependency AC-000")
		const readyCard = screen.getByLabelText("AC-003: Ready again")
		expect(readyCard).toHaveTextContent("ready")
		expect(readyCard).toHaveTextContent("1 prior rejection cycle")
		expect(readyCard).toHaveTextContent("1 failed attempt")
		expect(readyCard).toHaveTextContent("Execute")
	})

	it("opens a complete detail view and returns to the preserved board", async () => {
		showBoard({ review: ["AC-022"], ready: ["AC-013"] }, [
			{
				id: "AC-022",
				statementOfWork: {
					title: "Full ticket detail",
					objective: "Inspect the complete statement of work",
					context: "Cards intentionally omit detail",
					requirements: ["Show every durable field"],
					deliverables: ["Readable detail view"],
					constraints: ["Do not duplicate transcripts"],
					includedScope: ["Read-only presentation"],
					excludedScope: ["Unrelated Chat changes"],
					dependencies: ["AC-013", "AC-999"],
					acceptanceCriteria: ["Every field is represented"],
					validation: ["Component tests"],
				},
				lifecycle: {
					state: "review",
					completedAt: "2026-08-18T00:00:00.000Z",
					reviewComments: [
						{
							id: "r1",
							comment: "Please clarify the lock",
							author: "Customer",
							createdAt: "2026-08-18T01:00:00.000Z",
						},
					],
					blockedReasons: [
						{
							reason: "Waiting for approval",
							createdAt: "2026-08-17T00:00:00.000Z",
							resolvedAt: "2026-08-18T00:00:00.000Z",
						},
					],
					failedAttempts: [
						{
							historyItemId: "task-failed",
							summary: "Tests initially failed",
							failedAt: "2026-08-17T00:00:00.000Z",
						},
					],
				},
				execution: { historyItemIds: ["task-1"] },
			},
			{ id: "AC-013", statementOfWork: { title: "Board state" }, lifecycle: { state: "ready" } },
		])
		render(<BoardView />)

		const board = screen.getByLabelText("Kanban board")
		await userEvent.click(screen.getByLabelText("AC-022: Full ticket detail").querySelector("button")!)
		const detail = screen.getByTestId("ticket-detail-view")
		expect(detail).toHaveTextContent("Customer-authored statement of work")
		expect(detail).toHaveTextContent("Readable detail view")
		expect(detail).toHaveTextContent("Unrelated Chat changes")
		expect(detail).toHaveTextContent("Please clarify the lock")
		expect(detail).toHaveTextContent("Editing is unavailable while this ticket is review")
		expect(screen.getByRole("button", { name: "AC-013" })).toBeInTheDocument()
		expect(detail).toHaveTextContent("AC-999 (missing or permanently deleted)")
		expect(screen.getByRole("button", { name: "Open task task-1" })).toBeInTheDocument()

		await userEvent.click(screen.getByRole("button", { name: "← Back to board" }))
		expect(screen.queryByTestId("ticket-detail-view")).not.toBeInTheDocument()
		expect(screen.getByLabelText("Kanban board")).toBe(board)
	})

	it("represents optional and lifecycle data as absent and opens archived tickets", async () => {
		showBoard({ backlog: ["AC-001"] }, [{ id: "AC-001", statementOfWork: { title: "Minimal ticket" } }])
		render(<BoardView />)
		await userEvent.click(screen.getByRole("button", { name: "Open archived ticket AC-ARCHIVED" }))

		expect(screen.getByTestId("ticket-detail-view")).toHaveTextContent("Archived ticket")
	})
})
