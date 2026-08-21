export interface ProductionTestDesignFixture {
	name: string
	requiredGuidance: string[]
}

/**
 * Representative evidence-design decisions. These prompt-contract fixtures
 * ensure the bundled skill supplies the principles needed to choose a boundary;
 * they do not replace tests of the production skill resolver.
 */
export const productionTestDesignFixtures: ProductionTestDesignFixture[] = [
	{
		name: "pure production decision uses a unit boundary",
		requiredGuidance: ["package-local unit test", "production logic that can be exercised directly"],
	},
	{
		name: "internal module contract uses an integration boundary",
		requiredGuidance: ["integration test", "contracts between multiple real internal modules"],
	},
	{
		name: "webview form behavior uses the UI boundary",
		requiredGuidance: ["webview UI test", "React rendering, hooks, local component state, forms"],
	},
	{
		name: "extension-host workflow uses an end-to-end boundary",
		requiredGuidance: ["Use end-to-end only", "real extension host", "cross-process messaging"],
	},
	{
		name: "external nondeterministic service permits a documented substitute",
		requiredGuidance: [
			"genuinely external boundary",
			"explain why the real boundary cannot participate",
			"record what remains unverified",
		],
	},
	{
		name: "cross-boundary risk justifies mixed evidence",
		requiredGuidance: ["justified mix", "confidence across a material production boundary"],
	},
]
