module.exports = (output) => {
  let payload;
  try {
    payload = typeof output === "string" ? JSON.parse(output) : output;
  } catch (error) {
    return {
      pass: false,
      score: 0,
      reason: `local eval output is not JSON: ${error.message}`,
    };
  }

  if (!payload || payload.ok !== true) {
    return {
      pass: false,
      score: 0,
      reason: payload?.summary || "local eval case failed",
    };
  }

  return {
    pass: true,
    score: 1,
    reason: payload.summary || "local eval case passed",
  };
};
