export interface RootCauseAnalysisFixture {
	name: string
	requiredGuidance: string[]
}

/**
 * Representative diagnostic situations used as semantic prompt contracts.
 * In particular, the retry fixture ensures the skill does not confuse the
 * visible terminal failure with the upstream producer of the invalid state.
 */
export const rootCauseAnalysisFixtures: RootCauseAnalysisFixture[] = [
	{
		name: "retry exhaustion is a symptom of an upstream malformed request",
		requiredGuidance: [
			"retry exhaustion is insufficient",
			"what supplied the invalid state, decision, request, or assumption",
			"root cause from triggers, contributing conditions, propagation mechanisms, and visible symptoms",
		],
	},
	{
		name: "competing explanations require distinguishing observations",
		requiredGuidance: [
			"multiple plausible explanations",
			"outcomes distinguish the competing hypotheses",
			"predicted and actual result",
			"disconfirming evidence",
		],
	},
	{
		name: "confirmed cause precedes corrective change",
		requiredGuidance: [
			"Select a cause only when it explains all material observed evidence",
			"Confirm the selected cause with a targeted test",
			"Only after confirmation",
			"Guess-and-check modifications",
		],
	},
]
