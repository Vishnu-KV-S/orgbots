/** What an approval rule covers, in words — the Approvals section and a template's preview. */
export const RULE_ACTIONS = [
  ["*", "any action"],
  ["navigate", "opening a page"],
  ["click", "clicking"],
  ["type", "typing"],
  ["select", "choosing an option"],
  ["press", "pressing a key"],
  ["sign_in", "signing in with a saved login"],
  ["run_command", "running commands in the sandbox"],
  ["run_local", "running commands on this computer"],
  ["use_connector", "using a connected app (name it under On site)"],
] as const;

export const DECISION_LABEL = { ask: "Ask first", allow: "Allow", deny: "Never" } as const;

/** The action in a sentence — without the form's hints, like "(name it under On site)". */
export function ruleAction(actionType: string): string {
  const label = RULE_ACTIONS.find(([k]) => k === actionType)?.[1] ?? actionType;
  return label.replace(/ \(.*\)$/, "");
}
