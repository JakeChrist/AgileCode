import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import TicketAuthoringForm from "../TicketAuthoringForm"

const renderForm = (initialValues = {}) => {
	const props = { initialValues, onChange: vi.fn(), onCancel: vi.fn(), onSubmit: vi.fn() }
	render(<TicketAuthoringForm {...props} />)
	return props
}

describe("TicketAuthoringForm", () => {
	it("offers every customer-authored field without lifecycle metadata", () => {
		renderForm()
		for (const label of ["Title", "Objective", "Context"]) expect(screen.getByLabelText(label)).toBeInTheDocument()
		for (const label of [
			"Requirements",
			"Deliverables",
			"Constraints",
			"Included scope",
			"Excluded scope",
			"Dependencies",
			"Acceptance criteria",
			"Validation expectations",
		])
			expect(screen.getByRole("group", { name: new RegExp(label, "i") })).toBeInTheDocument()
		expect(screen.queryByLabelText(/created|workflow|archive|execution/i)).not.toBeInTheDocument()
	})

	it("adds, edits, reorders, and removes list entries without losing text", async () => {
		const user = userEvent.setup()
		renderForm({ requirements: ["First", "Second"] })
		await user.click(screen.getByRole("button", { name: "Add requirements" }))
		fireEvent.change(screen.getByLabelText("Requirements 3"), { target: { value: "Third" } })
		await user.click(screen.getByRole("button", { name: "Move Requirements 3 up" }))
		expect(screen.getByLabelText("Requirements 2")).toHaveValue("Third")
		expect(screen.getByLabelText("Requirements 3")).toHaveValue("Second")
		await user.click(screen.getByRole("button", { name: "Remove Requirements 1" }))
		expect(screen.getByLabelText("Requirements 1")).toHaveValue("Third")
	})

	it("saves a title-only backlog draft without requiring readiness fields", async () => {
		const user = userEvent.setup()
		const props = renderForm()
		fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Manual ticket" } })
		await user.click(screen.getByRole("button", { name: "Save to backlog" }))
		expect(screen.getByDisplayValue("Manual ticket")).toBeInTheDocument()
		expect(props.onSubmit).toHaveBeenCalledWith(
			expect.objectContaining({ title: "Manual ticket", objective: "", requirements: [] }),
		)
	})

	it("associates structural validation feedback with the deficient field", async () => {
		const props = renderForm()
		await userEvent.click(screen.getByRole("button", { name: "Save to backlog" }))

		const title = screen.getByRole("textbox", { name: /Title/ })
		expect(title).toHaveAttribute("aria-invalid", "true")
		expect(title).toHaveAccessibleDescription("String must contain at least 1 character(s)")
		expect(props.onSubmit).not.toHaveBeenCalled()
	})

	it("keeps authored values visible and reports a persistence failure", () => {
		render(
			<TicketAuthoringForm
				initialValues={{ title: "Keep this draft" }}
				onChange={vi.fn()}
				onCancel={vi.fn()}
				onSubmit={vi.fn()}
				submitError="Unable to write board.json"
			/>,
		)
		expect(screen.getByLabelText("Title")).toHaveValue("Keep this draft")
		expect(screen.getByRole("alert")).toHaveTextContent("Unable to write board.json")
	})

	it("protects dirty drafts on close and browser navigation", async () => {
		const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
		const props = renderForm()
		fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Keep me" } })
		const event = new Event("beforeunload", { cancelable: true })
		await waitFor(() => {
			window.dispatchEvent(event)
			expect(event.defaultPrevented).toBe(true)
		})
		await userEvent.click(screen.getByRole("button", { name: "Close ticket form" }))
		expect(confirm).toHaveBeenCalled()
		expect(props.onCancel).not.toHaveBeenCalled()
	})

	it("submits the complete statement of work and uses a responsive one-column baseline", async () => {
		const props = renderForm({
			title: "Ticket",
			objective: "Objective",
			context: "Context",
			requirements: ["Requirement"],
			constraints: ["Constraint"],
			includedScope: ["Scope"],
			acceptanceCriteria: ["Acceptance"],
			validation: ["Test"],
		})
		await userEvent.click(screen.getByRole("button", { name: "Save to backlog" }))
		expect(props.onSubmit).toHaveBeenCalledWith(expect.objectContaining({ title: "Ticket", validation: ["Test"] }))
		expect(screen.getByLabelText("Title").closest(".grid-cols-1")).toHaveClass("md:grid-cols-2")
	})
})
