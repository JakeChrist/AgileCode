import { ticketIdSchema, type Ticket } from "@roo-code/types"

export type DependencyStatus = "completed" | "unresolved" | "archived-completed" | "archived-unresolved" | "missing"

/** Resolves the effective state retained by an archived prerequisite. */
export function dependencyStatus(ticket: Ticket | undefined): DependencyStatus {
	if (!ticket) return "missing"
	if (ticket.lifecycle.state === "done") return "completed"
	if (ticket.lifecycle.state === "archived") {
		return ticket.lifecycle.archivedFrom === "done" ? "archived-completed" : "archived-unresolved"
	}
	return "unresolved"
}

/** Validates board-local references and the complete proposed dependency graph. */
export function validateTicketDependencies(
	id: string,
	dependencies: readonly string[],
	tickets: readonly Ticket[],
): void {
	if (new Set(dependencies).size !== dependencies.length) throw new Error("Dependencies must not contain duplicates")
	for (const dependency of dependencies) {
		if (!ticketIdSchema.safeParse(dependency).success) {
			throw new Error(`Dependency ${dependency} is not a board-local ticket identifier`)
		}
	}
	if (dependencies.includes(id)) throw new Error(`Ticket ${id} cannot depend on itself`)

	const known = new Set(tickets.map((ticket) => ticket.id))
	const missing = dependencies.filter((dependency) => !known.has(dependency))
	if (missing.length) throw new Error(`Unknown dependencies: ${missing.join(", ")}`)

	const graph = new Map(tickets.map((ticket) => [ticket.id, ticket.statementOfWork.dependencies] as const))
	graph.set(id, [...dependencies])
	const visit = (current: string, path: Set<string>): boolean => {
		if (path.has(current)) return true
		const nextPath = new Set(path).add(current)
		return (graph.get(current) ?? []).some((dependency) => visit(dependency, nextPath))
	}
	if (visit(id, new Set())) throw new Error(`Dependencies for ${id} would create a cycle`)
}
