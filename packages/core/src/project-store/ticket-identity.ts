import { randomBytes } from "crypto"

import { ticketIdSchema } from "@roo-code/types"

const DEFAULT_PREFIX = "AC"
const MAX_SLUG_LENGTH = 80

/** Allocate an opaque identifier. Entropy, rather than board order, makes merge collisions negligible. */
export function generateTicketId(prefix = DEFAULT_PREFIX, bytes: () => Buffer = () => randomBytes(10)): string {
	const normalizedPrefix = prefix.trim().toUpperCase()
	const id = `${normalizedPrefix}-${bytes().toString("hex").toUpperCase()}`
	return ticketIdSchema.parse(id)
}

/**
 * Produce a portable, readable record basename. The result deliberately contains
 * only ASCII letters, digits, and hyphens and is safe on Windows, macOS, and Linux.
 */
export function createTicketStorageName(id: string, title: string): string {
	const parsedId = ticketIdSchema.parse(id)
	const slug = title
		.normalize("NFKD")
		.replace(/[\u0300-\u036f]/g, "")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "")
		.slice(0, MAX_SLUG_LENGTH)
		.replace(/-+$/g, "")
	return slug ? `${parsedId}-${slug}` : parsedId
}
