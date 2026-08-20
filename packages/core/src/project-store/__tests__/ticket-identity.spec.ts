import { describe, expect, it } from "vitest"

import { createTicketStorageName, generateTicketId } from "../ticket-identity.js"

describe("ticket identity", () => {
	it("generates stable-shape collision-resistant identifiers independently of board position", () => {
		const first = generateTicketId("AC", () => Buffer.from("00112233445566778899", "hex"))
		const second = generateTicketId("AC", () => Buffer.from("0011223344556677889a", "hex"))
		expect(first).toBe("AC-00112233445566778899")
		expect(second).not.toBe(first)
	})

	it.each([
		["Implement Login: Windows / macOS?", "AC-ABC123-implement-login-windows-macos"],
		[" Résumé 🚀 ", "AC-ABC123-resume"],
		["...", "AC-ABC123"],
	])("creates a portable storage name for %j", (title, expected) => {
		expect(createTicketStorageName("AC-ABC123", title)).toBe(expected)
	})
})
