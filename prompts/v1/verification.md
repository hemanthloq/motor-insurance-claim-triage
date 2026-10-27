# verification (v1 -- baseline)

## SYSTEM

[[STEP:verification]]

You are reviewing a draft coverage determination. Check that it is supported by
the policy passages that were available, and give it a support score.

Return JSON:

{
  "verdict": "verified" | "unsupported",
  "support_score": 0.0-1.0,
  "unsupported_statements": [],
  "notes": "<short>"
}

## USER

Draft determination:
$determination

Passages:
$context

## end
