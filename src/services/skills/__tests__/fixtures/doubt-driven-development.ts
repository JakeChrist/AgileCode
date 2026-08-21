export interface DoubtDrivenDevelopmentFixture {
	name: string
	hiddenAssumption: string
	requiredGuidance: string[]
}

/**
 * A representative hidden assumption whose answer changes implementation
 * direction, plus the targeted guidance needed to resolve it.
 */
export const doubtDrivenDevelopmentFixtures: DoubtDrivenDevelopmentFixture[] = [
	{
		name: "parser replacement assumes a single input encoding",
		hiddenAssumption: "the input is always UTF-8",
		requiredGuidance: [
			"unverified assumption",
			"change the parser choice, compatibility requirements, and regression tests",
			"focused inspection of the input contract and representative fixtures or telemetry",
			"ask the contract owner whether non-UTF-8 input must remain supported",
			"Keep the replacement conditional",
		],
	},
]
