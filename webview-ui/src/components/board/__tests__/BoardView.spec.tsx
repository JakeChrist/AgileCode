import userEvent from "@testing-library/user-event"

import { render, screen } from "@/utils/test-utils"
import { vscode } from "@/utils/vscode"

import BoardView from "../BoardView"

const mockUseBoardState = vi.fn()
const mockUseExtensionState = vi.fn()

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
	mockUseExtensionState.mockReturnValue({ cwd: "/workspace/agile-code" })
	mockUseBoardState.mockReturnValue({
		status: "ready",
		selectedBoard: {
			scope: { id: "git:board", kind: "git", rootPath: "/workspace/agile-code" },
			snapshot: {
				board: { columns: columns(columnOverrides), archiveOrder: ["AC-ARCHIVED"] },
				activeTickets,
				archivedTickets: [{ id: "AC-ARCHIVED", statementOfWork: { title: "Archived ticket" } }],
			},
		},
	})
}

describe("BoardView", () => {
	afterEach(() => vi.unstubAllGlobals())

	it("renders the selected repository and its shared board snapshot", () => {
		showBoard({ backlog: ["AC-014"] }, [{ id: "AC-014", statementOfWork: { title: "Add Board navigation" } }])

		render(<BoardView />)

		expect(screen.getByTitle("/workspace/agile-code")).toHaveTextContent("/workspace/agile-code")
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
			"In progress column",
			"Blocked column",
			"Review column",
			"Done column",
		])
		expect(screen.queryByText("Archived ticket")).not.toBeInTheDocument()
		expect(screen.queryByLabelText("Archived column")).not.toBeInTheDocument()
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
			Array.from(reviewList.querySelectorAll("article")).map((card) => card.firstElementChild?.textContent),
		).toEqual(["AC-003", "AC-001", "AC-002"])
	})

	it("keeps high-volume ticket lists independently scrollable beneath their headers", () => {
		const ids = Array.from({ length: 100 }, (_, index) => `AC-${String(index + 1).padStart(3, "0")}`)
		showBoard(
			{ in_progress: ids },
			ids.map((id) => ({ id, statementOfWork: { title: id } })),
		)
		render(<BoardView />)

		const board = screen.getByLabelText("Kanban board")
		const column = screen.getByLabelText("In progress column")
		const ticketList = screen.getByLabelText("In progress tickets")
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
			"In progress (0)",
			"Blocked (0)",
			"Review (1)",
			"Done (0)",
		])
		expect(screen.getAllByRole("region")).toHaveLength(1)

		await userEvent.selectOptions(selector, "review")
		expect(screen.getByLabelText("Review column")).toHaveTextContent("Compact sidebar board")
		expect(screen.getAllByRole("region")).toHaveLength(1)
	})

	it("offers the primary move operation on compact ticket cards", async () => {
		const postMessage = vi.spyOn(vscode, "postMessage")
		vi.stubGlobal("matchMedia", () => ({
			matches: true,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		}))
		showBoard({ backlog: ["AC-019"] }, [{ id: "AC-019", statementOfWork: { title: "Compact sidebar board" } }])
		render(<BoardView />)

		await userEvent.selectOptions(screen.getByLabelText("Move AC-019 to"), "ready")
		expect(postMessage).toHaveBeenCalledWith(
			expect.objectContaining({
				type: "board_request",
				request: expect.objectContaining({
					boardId: "git:board",
					operation: "move_ticket",
					ticketId: "AC-019",
					destination: "ready",
					position: 0,
				}),
			}),
		)
	})
})
