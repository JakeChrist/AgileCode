export interface AgileTicketCreationFixture {
	name: string
	request: string
	requiredGuidance: string[]
}

/**
 * Representative inputs used to keep the bundled prompt's quality contract
 * explicit. These are prompt-contract fixtures, not generated ticket output.
 */
export const agileTicketCreationFixtures: AgileTicketCreationFixture[] = [
	{
		name: "vague outcome",
		request: "Make onboarding better.",
		requiredGuidance: ["actual desired user or project outcome", "Objective, observable pass/fail conditions"],
	},
	{
		name: "multiple independent scopes",
		request: "Add account export and redesign the billing page.",
		requiredGuidance: ["independently valuable or independently deliverable outcomes", "separate tickets"],
	},
	{
		name: "dependency",
		request: "Ship SSO after the identity provider contract is approved.",
		requiredGuidance: ["why each matters", "whether it blocks readiness"],
	},
	{
		name: "validation",
		request: "Create a ticket for preserving saved filters during an upgrade.",
		requiredGuidance: ["Preserved Behavior", "narrowest effective test layer", "regression evidence"],
	},
]
