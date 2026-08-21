import { promises as fs } from "node:fs"
import path from "node:path"

export interface TicketContextEvidence {
	path: string
	content: string
}

export interface PreparedTicketContext {
	status: "inspected" | "unavailable"
	evidence: TicketContextEvidence[]
	unresolvedQuestions: string[]
	limitations: string[]
}

const SKIPPED_DIRECTORIES = new Set([
	".git",
	".agilecode",
	"node_modules",
	"dist",
	"build",
	"out",
	"coverage",
	".turbo",
])
const PROJECT_FILES = new Set([
	"agents.md",
	"readme.md",
	"package.json",
	"pnpm-workspace.yaml",
	"pyproject.toml",
	"cargo.toml",
	"go.mod",
	"makefile",
	"vitest.config.ts",
	"jest.config.js",
	"pytest.ini",
])
const MAX_DISCOVERED_FILES = 2_000
const MAX_EVIDENCE_FILES = 12
const MAX_FILE_CHARACTERS = 4_000
const MAX_CONTEXT_CHARACTERS = 24_000

function requestTerms(request: string): string[] {
	return [...new Set(request.toLowerCase().match(/[a-z][a-z0-9_-]{2,}/g) ?? [])]
		.filter((term) => !new Set(["the", "and", "for", "with", "from", "into", "this", "that", "improve"]).has(term))
		.slice(0, 12)
}

function relevance(relativePath: string, terms: string[]): number {
	const normalized = relativePath.toLowerCase()
	const base = path.basename(normalized)
	let score = PROJECT_FILES.has(base) ? 20 : 0
	if (/(^|\/)(__tests__|tests?|specs?)(\/|\.|$)/.test(normalized))
		score += terms.includes("test") || terms.includes("tests") ? 18 : 3
	for (const term of terms) if (normalized.includes(term)) score += 8
	if (/\.(ts|tsx|js|jsx|py|rs|go|java|json|ya?ml|toml|md)$/.test(normalized)) score += 1
	return score
}

async function discover(rootPath: string): Promise<{ files: string[]; truncated: boolean }> {
	const files: string[] = []
	const pending = [""]
	while (pending.length && files.length < MAX_DISCOVERED_FILES) {
		const relativeDirectory = pending.shift()!
		const entries = await fs.readdir(path.join(rootPath, relativeDirectory), { withFileTypes: true })
		for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
			if (entry.isSymbolicLink() || (entry.isDirectory() && SKIPPED_DIRECTORIES.has(entry.name))) continue
			const relative = path.join(relativeDirectory, entry.name)
			if (entry.isDirectory()) pending.push(relative)
			else if (entry.isFile()) files.push(relative)
			if (files.length >= MAX_DISCOVERED_FILES) break
		}
	}
	return { files, truncated: pending.length > 0 }
}

/** Builds a small, read-only evidence packet for defining a ticket; it never writes to the repository. */
export async function prepareTicketContext(rootPath: string, roughRequest: string): Promise<PreparedTicketContext> {
	try {
		const stat = await fs.stat(rootPath)
		if (!stat.isDirectory()) throw new Error("scope root is not a directory")
		const { files, truncated } = await discover(rootPath)
		const terms = requestTerms(roughRequest)
		const selected = files
			.map((file) => ({ file, score: relevance(file, terms) }))
			// A common source extension alone is not enough to make a file relevant.
			.filter(({ score }) => score > 1)
			.sort((a, b) => b.score - a.score || a.file.localeCompare(b.file))
			.slice(0, MAX_EVIDENCE_FILES)
		let remaining = MAX_CONTEXT_CHARACTERS
		const evidence: TicketContextEvidence[] = []
		for (const { file } of selected) {
			if (remaining <= 0) break
			const raw = await fs.readFile(path.join(rootPath, file), "utf8")
			const content = raw.slice(0, Math.min(MAX_FILE_CHARACTERS, remaining))
			remaining -= content.length
			evidence.push({ path: file.split(path.sep).join("/"), content })
		}
		const unresolvedQuestions = evidence.length
			? []
			: ["Which repository files or project conventions define the requested behavior?"]
		const limitations = [
			...(truncated ? [`File discovery was capped at ${MAX_DISCOVERED_FILES} files.`] : []),
			...(selected.length > evidence.length
				? [`Evidence content was capped at ${MAX_CONTEXT_CHARACTERS} characters.`]
				: []),
		]
		return { status: "inspected", evidence, unresolvedQuestions, limitations }
	} catch (error) {
		return {
			status: "unavailable",
			evidence: [],
			unresolvedQuestions: ["What repository context is required to make this request concrete?"],
			limitations: [
				`Repository inspection was unavailable: ${error instanceof Error ? error.message : String(error)}`,
			],
		}
	}
}
