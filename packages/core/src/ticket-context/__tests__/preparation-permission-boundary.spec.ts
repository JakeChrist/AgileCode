import { describe, expect, it, vi } from "vitest"

import { PreparationPermissionError, TicketPreparationPermissionBoundary } from "../preparation-permission-boundary.js"

describe("TicketPreparationPermissionBoundary", () => {
	const prohibited = [
		["editProjectFiles", "file_edit"],
		["runSideEffectCommand", "side_effect_command"],
		["startTask", "task_start"],
		["mutateBoard", "board_mutation"],
	] as const

	it.each(prohibited)("rejects and reports %s", (method, action) => {
		const boundary = new TicketPreparationPermissionBoundary()

		try {
			boundary[method]()
			expect.fail("the prohibited preparation action was accepted")
		} catch (error) {
			expect(error).toBeInstanceOf(PreparationPermissionError)
			expect((error as PreparationPermissionError).toReport()).toEqual({
				code: "preparation_permission_denied",
				action,
				message: expect.stringContaining(action.replaceAll("_", " ")),
			})
		}
	})

	it("allows a read-only inspection", async () => {
		const inspect = vi.fn().mockResolvedValue(["src/index.ts"])
		const boundary = new TicketPreparationPermissionBoundary()

		await expect(boundary.inspect(inspect)).resolves.toEqual(["src/index.ts"])
		expect(inspect).toHaveBeenCalledOnce()
	})
})
