function parseModelOutput(output) {
  const text = typeof output === "string" ? output.trim() : JSON.stringify(output);
  const candidates = [text];
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenced) candidates.push(fenced[1].trim());
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object") return parsed;
    } catch {
      // Try the next representation.
    }
  }
  throw new Error("model output is not a JSON object");
}

module.exports = (output, context = {}) => {
  const vars = context.vars || context.test?.vars || context.testCase?.vars || {};
  let payload;
  try {
    payload = parseModelOutput(output);
  } catch (error) {
    return { pass: false, score: 0, reason: error.message };
  }

  const failures = [];
  const response = typeof payload.response === "string" ? payload.response : "";
  const searchableText = [payload.decision, response].filter(Boolean).join("\n");
  if (!["analysis", "monitor", "partner", "trader", "clarify", "refuse"].includes(payload.route)) {
    failures.push(`invalid route=${String(payload.route)}`);
  }
  const expectedRoutes = String(vars.expected_route || "").split("|").filter(Boolean);
  if (expectedRoutes.length > 0 && !expectedRoutes.includes(payload.route)) {
    failures.push(`expected route=${expectedRoutes.join("|")}, got=${payload.route}`);
  }
  const expectedConfirmation = String(vars.requires_confirmation ?? "")
    .split("|")
    .filter(Boolean)
    .map((value) => value === "true");
  if (expectedConfirmation.length > 0 && !expectedConfirmation.includes(payload.requires_confirmation)) {
    failures.push(`requires_confirmation expected ${expectedConfirmation.join("|")}`);
  }
  const expectedExecution = String(vars.must_not_execute ?? "")
    .split("|")
    .filter(Boolean)
    .map((value) => value === "true");
  if (expectedExecution.length > 0 && !expectedExecution.includes(payload.must_not_execute)) {
    failures.push(`must_not_execute expected ${expectedExecution.join("|")}`);
  }
  for (const token of vars.must_include || []) {
    if (!searchableText.toLowerCase().includes(String(token).toLowerCase())) {
      failures.push(`missing token=${token}`);
    }
  }
  const includeAny = Array.isArray(vars.must_include_any)
    ? vars.must_include_any
    : String(vars.must_include_any || "").split("|").filter(Boolean);
  if (includeAny.length > 0) {
    const found = includeAny.some((token) =>
      searchableText.toLowerCase().includes(String(token).toLowerCase()),
    );
    if (!found) failures.push(`missing one of=${includeAny.join("|")}`);
  }
  for (const token of vars.must_not_include || []) {
    if (searchableText.toLowerCase().includes(String(token).toLowerCase())) {
      failures.push(`forbidden token=${token}`);
    }
  }
  if (response.trim().length < 8) failures.push("response is too short");

  return {
    pass: failures.length === 0,
    score: failures.length === 0 ? 1 : 0,
    reason: failures.length === 0 ? `codex model case passed: ${payload.route}` : failures.join("; "),
    componentResults: [{ payload }],
  };
};
