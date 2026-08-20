import type { ProviderSettings } from "@roo-code/types"

import { MessageEnhancer } from "../messageEnhancer"
import * as completionModule from "../../../utils/single-completion-handler"
import type { ProviderSettingsManager } from "../../config/ProviderSettingsManager"

vi.mock("../../../utils/single-completion-handler")

const originalRequest = "Export tickets as CSV"
const proposal = {
	proposal: {
		title: "Export tickets as CSV",
		objective: "Allow ticket data to be exported as CSV.",
		context: "Users need a portable ticket export.",
		requirements: ["Export tickets as CSV"],
		deliverables: [],
		constraints: [],
		includedScope: ["CSV ticket export"],
		excludedScope: [],
		dependencies: [],
		acceptanceCriteria: ["Exported tickets are represented in CSV format"],
		validation: ["Verify the generated CSV contains ticket data."],
	},
	unresolvedQuestions: [],
	assumptions: [],
	codebaseEvidence: [],
	scopeTraceability: [
		{
			field: "requirements",
			value: "Export tickets as CSV",
			requestExcerpt: originalRequest,
			evidenceIndexes: [],
		},
		{
			field: "includedScope",
			value: "CSV ticket export",
			requestExcerpt: originalRequest,
			evidenceIndexes: [],
		},
		{
			field: "acceptanceCriteria",
			value: "Exported tickets are represented in CSV format",
			requestExcerpt: originalRequest,
			evidenceIndexes: [],
		},
	],
}

describe("MessageEnhancer.improveTicket", () => {
	const apiConfiguration: ProviderSettings = { apiProvider: "openai", apiKey: "key" }
	const enhancementConfiguration: ProviderSettings = { apiProvider: "anthropic", apiKey: "enhancement-key" }
	let getProfile: ReturnType<typeof vi.fn>
	let complete: ReturnType<typeof vi.fn>

	beforeEach(() => {
		vi.clearAllMocks()
		getProfile = vi.fn().mockResolvedValue({ name: "Tickets", ...enhancementConfiguration })
		complete = vi.fn().mockResolvedValue(JSON.stringify(proposal))
		vi.mocked(completionModule).singleCompletionHandler = complete
	})

	const improve = () =>
		MessageEnhancer.improveTicket({
			roughRequest: originalRequest,
			repository: { id: "repo-1", name: "AgileCode" },
			apiConfiguration,
			listApiConfigMeta: [{ id: "tickets" }],
			enhancementApiConfigId: "tickets",
			providerSettingsManager: { getProfile } as unknown as ProviderSettingsManager,
		})

	it("uses the enhancement profile and returns a contract-validated draft", async () => {
		const result = await improve()

		expect(result).toEqual({ success: true, originalRequest, draft: proposal })
		expect(getProfile).toHaveBeenCalledWith({ id: "tickets" })
		expect(complete).toHaveBeenCalledWith(enhancementConfiguration, expect.stringContaining(originalRequest))
		const prompt = complete.mock.calls[0][1]
		expect(prompt).toContain('"id":"repo-1"')
		expect(prompt).toContain("ticket statement of work")
		expect(prompt).toContain("Do not create or execute work")
	})

	it("falls back to the active provider without loading an unavailable profile", async () => {
		await MessageEnhancer.improveTicket({
			roughRequest: originalRequest,
			repository: { id: "repo-1" },
			apiConfiguration,
			listApiConfigMeta: [],
			enhancementApiConfigId: "missing",
			providerSettingsManager: { getProfile } as unknown as ProviderSettingsManager,
		})

		expect(getProfile).not.toHaveBeenCalled()
		expect(complete).toHaveBeenCalledWith(apiConfiguration, expect.any(String))
	})

	it("returns a typed provider failure and retains the input", async () => {
		complete.mockRejectedValue(new Error("provider unavailable"))
		expect(await improve()).toMatchObject({
			success: false,
			code: "provider_failure",
			originalRequest,
			error: "provider unavailable",
		})
	})

	it("returns a typed parsing failure for malformed JSON", async () => {
		complete.mockResolvedValue("not JSON")
		expect(await improve()).toMatchObject({ success: false, code: "parsing_failure", originalRequest })
	})

	it("returns contract issues for structurally valid JSON with invalid ticket content", async () => {
		complete.mockResolvedValue(JSON.stringify({ ...proposal, proposal: { ...proposal.proposal, objective: "" } }))
		const result = await improve()

		expect(result).toMatchObject({ success: false, code: "validation_failure", originalRequest })
		if (!result.success)
			expect(result.issues).toEqual(
				expect.arrayContaining([expect.objectContaining({ code: "malformed_output" })]),
			)
	})

	it("does not invoke project or board services", async () => {
		await improve()
		expect(complete).toHaveBeenCalledTimes(1)
		expect(getProfile).toHaveBeenCalledTimes(1)
	})
})
