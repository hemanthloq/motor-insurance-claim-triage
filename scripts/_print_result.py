"""Print a triage result on the terminal, for the demonstration scripts.

    curl -s .../result | python scripts/_print_result.py

Shows the recommendation, every citation with the clause it resolves to, the
escalation path if one was taken, and the run accounting -- which between them
are demonstration items 2, 3 and 10.
"""

from __future__ import annotations

import json
import sys

RULE = "=" * 78


def main() -> int:
    try:
        result = json.load(sys.stdin)
    except Exception as exc:
        print(f"could not parse the result: {exc}", file=sys.stderr)
        return 1

    coverage = result.get("coverage") or {}
    routing = result.get("routing") or {}
    verification = result.get("verification") or {}
    escalation = result.get("escalation") or {}
    cost = result.get("cost") or {}

    print()
    print(RULE)
    print(f"  status      {result.get('status')}")
    print(f"  position    {coverage.get('position')}")
    print(
        f"  queue       {routing.get('queue')}"
        f"  (priority {routing.get('priority')},"
        f" first touch {routing.get('target_first_touch_hours')}h)"
    )
    print(
        f"  verified    {verification.get('verdict')}"
        f"  support {verification.get('support_score')}"
        f"  citations {verification.get('citations_resolved')}"
        f"/{verification.get('citations_checked')}"
    )
    if result.get("degraded"):
        print(f"  degraded    {' -> '.join(result.get('degradation_path') or [])}")
    print(RULE)
    print()

    if coverage.get("summary"):
        print(f"  {coverage['summary']}")
        print()
    if coverage.get("reasoning"):
        print(f"  {coverage['reasoning']}")
        print()

    if coverage.get("open_questions"):
        print("  a handler must establish:")
        for question in coverage["open_questions"]:
            print(f"    - {question}")
        print()

    if routing.get("reason"):
        print(f"  routing reason: {routing['reason']}")
    if routing.get("triggered_rules"):
        print(f"  rules fired:    {', '.join(routing['triggered_rules'])}")
    print()

    citations = result.get("citations") or []
    print(f"  citations ({len(citations)}):")
    if not citations:
        print("    none resolved -- which is why this run escalated rather than answering")
    for citation in citations:
        print(f"    - {citation.get('document_title')}")
        print(
            f"      {citation.get('section_path')}"
            f"  (page {citation.get('page')}, {citation.get('role')})"
        )
        quote = (citation.get("quote") or "").strip()
        print(f'      "{quote[:220]}"')
        print(f"      {citation.get('clause_id')}")
    print()

    if escalation.get("required"):
        print(f"  ESCALATED  {escalation.get('reason')}")
        print(f"             {escalation.get('explanation')}")
        print(f"             assigned to {escalation.get('assigned_to')}")
        print()

    if result.get("guardrail_events"):
        kinds = sorted({e.get("kind") for e in result["guardrail_events"]})
        print(f"  guardrail events: {len(result['guardrail_events'])} ({', '.join(kinds)})")
        print()

    print(
        f"  steps {len(result.get('steps') or [])}"
        f"  tool calls {result.get('tool_calls')}"
        f"  tokens {cost.get('total_tokens')}"
        f"  cost USD {cost.get('cost_usd')}"
        f"  {result.get('duration_seconds')}s"
    )
    if cost.get("largest_driver"):
        print(f"  largest cost driver: {cost['largest_driver']}")
    versions = result.get("prompt_versions") or {}
    if versions:
        print(f"  prompts: {', '.join(sorted(versions.values()))}")
    print(f"  index {result.get('index_version')}   trace {result.get('trace_id')}")
    print()

    steps = result.get("steps") or []
    if steps:
        print("  step trace")
        for step in steps:
            print(
                f"    {step['index']:>2}. {step['agent']:<17} {step['step']:<22}"
                f" {step['outcome']:<18} {step['duration_ms']:>8.0f}ms"
                f"  {(step.get('prompt_version') or '')}"
            )
            if step.get("tool_query"):
                print(f"        query: {step['tool_query'][:150]}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
