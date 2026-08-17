import { render, screen } from "@/utils/test-utils"

import BoardView from "../BoardView"

const mockUseBoardState = vi.fn()
const mockUseExtensionState = vi.fn()

vi.mock("@/context/BoardStateContext", () => ({ useBoardState: () => mockUseBoardState() }))
vi.mock("@/context/ExtensionStateContext", () => ({ useExtensionState: () => mockUseExtensionState() }))

describe("BoardView", () => {
	it("renders the selected repository and its shared board snapshot", () => {
		mockUseExtensionState.mockReturnValue({ cwd: "/workspace/agile-code" })
		mockUseBoardState.mockReturnValue({
			status: "ready",
			selectedBoard: {
				snapshot: {
					board: {
						columns: { backlog: ["AC-014"], ready: [], in_progress: [], blocked: [], review: [], done: [] },
					},
					activeTickets: [{ id: "AC-014", statementOfWork: { title: "Add Board navigation" } }],
				},
			},
		})

		render(<BoardView />)

		expect(screen.getByTitle("/workspace/agile-code")).toHaveTextContent("/workspace/agile-code")
		expect(screen.getByText("AC-014")).toBeInTheDocument()
		expect(screen.getByText("Add Board navigation")).toBeInTheDocument()
		expect(screen.getByLabelText("Kanban board")).toBeInTheDocument()
	})
})
