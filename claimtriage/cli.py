"""Command-line tools (D2, L2, L3).

    python -m claimtriage.cli corpus-fetch        download the manifest sources
    python -m claimtriage.cli ingest              parse, chunk, embed, index
    python -m claimtriage.cli index-info          freshness and composition
    python -m claimtriage.cli search "..."        query the index by hand
    python -m claimtriage.cli clause <id>         print one clause
    python -m claimtriage.cli eval                run the evaluation set
    python -m claimtriage.cli compare a.json b.json
    python -m claimtriage.cli prompt-compare      run both prompt sets and diff
    python -m claimtriage.cli export-contracts    write JSON Schema to disk
    python -m claimtriage.cli triage <file.json>  run one FNOL end to end
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from .logging_setup import configure_logging, get_logger

app = typer.Typer(add_completion=False, help="claim-triage operator tools")
log = get_logger("cli")


@app.callback()
def _setup() -> None:
    configure_logging("cli")


# -----------------------------------------------------------------------------
# Corpus (D2)
# -----------------------------------------------------------------------------


@app.command("corpus-fetch")
def corpus_fetch(
    manifest: str = typer.Option(None, help="Path to corpus/sources.yaml"),
    force: bool = typer.Option(False, help="Re-download even if the file is present"),
) -> None:
    """Download every enabled source in the manifest into corpus/raw/."""
    from .retrieval.ingest import fetch_missing, load_manifest

    specs = load_manifest(manifest)
    fetched = fetch_missing(specs, force=force)
    for spec in specs:
        status = "fetched" if spec.document_id in fetched else (
            "present" if spec.path.exists() else "MISSING"
        )
        typer.echo(f"  {status:8s}  {spec.document_id}  ->  {spec.path}")
    typer.echo(f"\n{len(fetched)} downloaded, {len(specs)} enabled in the manifest.")


@app.command("ingest")
def ingest(
    manifest: str = typer.Option(None, help="Path to corpus/sources.yaml"),
    force: bool = typer.Option(False, help="Re-index even if the content hash is unchanged"),
    fetch: bool = typer.Option(False, help="Download missing sources first"),
) -> None:
    """Parse, chunk, embed and index the corpus. Idempotent."""
    from .retrieval.ingest import fetch_missing, load_manifest, run_ingest

    if fetch:
        fetch_missing(load_manifest(manifest))
    meta = run_ingest(manifest=manifest, force=force)
    typer.echo(json.dumps({k: v for k, v in meta.items() if k != "documents"}, indent=2, default=str))
    for document in meta.get("documents", []):
        typer.echo(
            f"  {document['document_id']}: {document.get('pages')} pages, "
            f"{document.get('chunks')} chunks, roles={document.get('roles')}"
        )
    if meta.get("failures"):
        typer.echo("\nFAILURES:")
        for failure in meta["failures"]:
            typer.echo(f"  {failure['document_id']}: {failure['error']}")
        raise typer.Exit(code=1)


@app.command("index-info")
def index_info() -> None:
    """Data freshness and index composition."""
    from .retrieval.index import get_index

    index = get_index()
    meta = index.metadata()
    meta["chunks_live"] = index.size
    typer.echo(json.dumps(meta, indent=2, default=str))


@app.command("search")
def search(
    query: str,
    top_k: int = typer.Option(5, "--top-k", "-k"),
    role: Optional[str] = typer.Option(None, help="grant|restriction|exclusion|condition|procedure|definition"),
    strategy: str = typer.Option("hybrid", help="hybrid|dense|lexical"),
) -> None:
    """Query the index directly. Useful for showing what a scenario depends on."""
    from .retrieval.index import get_index

    hits = get_index().search(query, top_k=top_k, role=role, strategy=strategy)
    if not hits:
        typer.echo("no results")
        raise typer.Exit(code=1)
    for hit in hits:
        typer.echo(f"\n[{hit.score:.3f}] {hit.role:11s} p{hit.page}  {hit.clause_id}")
        typer.echo(f"  {hit.section_path}")
        typer.echo(f"  {hit.text[:400]}")
        if hit.references:
            typer.echo(f"  references: {hit.references}")


@app.command("clause")
def clause(clause_id: str, neighbours: int = typer.Option(0, "--neighbours", "-n")) -> None:
    """Print one clause, optionally with its neighbours."""
    from .retrieval.index import get_index

    index = get_index()
    hit = index.get_clause(clause_id)
    if not hit:
        typer.echo(f"unknown clause {clause_id}")
        raise typer.Exit(code=1)
    typer.echo(json.dumps(hit.to_dict(), indent=2, default=str))
    for neighbour in index.neighbours(clause_id, window=neighbours) if neighbours else []:
        typer.echo(f"\n-- neighbour {neighbour.clause_id}\n{neighbour.text[:400]}")


# -----------------------------------------------------------------------------
# Evaluation (L3)
# -----------------------------------------------------------------------------


@app.command("eval")
def evaluate(
    dataset: str = typer.Option("eval/dataset.yaml"),
    out: str = typer.Option("eval/reports/latest.json", help="Where to write the report"),
    judge: bool = typer.Option(False, help="Add an LLM-as-judge scoring pass"),
    allow_stub: bool = typer.Option(False, help="Permit running against the stub provider"),
    only: Optional[str] = typer.Option(None, help="Comma-separated scenario ids"),
    prompt_set: Optional[str] = typer.Option(None, help="Override PROMPT_SET for this run"),
    label: str = typer.Option("", help="A label for the report"),
) -> None:
    """Run the fixed evaluation set and write a report."""
    from .eval.harness import StubProviderRefused, run_evaluation

    try:
        report = run_evaluation(
            dataset_path=dataset,
            out_path=out,
            judge=judge,
            allow_stub=allow_stub,
            only=[s.strip() for s in only.split(",")] if only else None,
            prompt_set=prompt_set,
            label=label,
        )
    except StubProviderRefused as exc:
        typer.echo(f"refused: {exc}")
        raise typer.Exit(code=2)

    _print_report(report)
    typer.echo(f"\nreport written to {out}")
    raise typer.Exit(code=0 if report["summary"]["failed"] == 0 else 1)


def _print_report(report: dict) -> None:
    summary, metrics = report["summary"], report["metrics"]
    typer.echo("\n" + "=" * 78)
    typer.echo(f"  {report['label']}")
    typer.echo("=" * 78)
    typer.echo(
        f"  scenarios {report['scenarios']}   passed {summary['passed']}   "
        f"failed {summary['failed']}   pass rate {summary['pass_rate']:.1%}"
    )
    typer.echo(
        f"  escalated {summary['escalated']}   rejected at boundary "
        f"{summary['rejected_at_boundary']}   agent runs {summary['agent_runs']}"
    )
    typer.echo("\n  metrics (lexical scorers)")
    for key in ("groundedness", "context_relevance", "faithfulness", "hallucination_rate"):
        typer.echo(f"    {key:22s} {metrics[key]:.4f}")
    if report.get("judge_metrics"):
        typer.echo("\n  metrics (LLM judge)")
        for key, value in report["judge_metrics"].items():
            typer.echo(f"    {key:22s} {value}")
    typer.echo(
        f"\n  mean steps {summary['mean_steps']}   mean tool calls "
        f"{summary['mean_tool_calls']}   mean duration {summary['mean_duration_seconds']}s"
    )
    typer.echo(
        f"  total tokens {summary['total_tokens']}   total cost "
        f"USD {summary['total_cost_usd']}"
    )
    if not report.get("valid_for_reporting"):
        typer.echo("\n  !! stub provider: these numbers are NOT valid for reporting")

    failures = [r for r in report["records"] if not r.get("passed")]
    if failures:
        typer.echo("\n  failures")
        for record in failures:
            typer.echo(f"    {record['id']}: {'; '.join(record.get('failures') or [])}")

    reference = report.get("reference_case")
    if reference:
        typer.echo(
            f"\n  section 4.1 reference case: "
            f"{'PASS' if reference['passed'] else 'FAIL'} "
            f"(position={reference.get('position')}, queue={reference.get('queue')})"
        )
        for citation in reference.get("citations") or []:
            typer.echo(f"      cited {citation['section_path']} (p{citation.get('page')})")


@app.command("compare")
def compare(before: str, after: str, out: Optional[str] = typer.Option(None)) -> None:
    """Diff two evaluation reports (L2, L3 before/after)."""
    from .eval.harness import compare_reports

    before_report = json.loads(Path(before).read_text(encoding="utf-8"))
    after_report = json.loads(Path(after).read_text(encoding="utf-8"))
    diff = compare_reports(before_report, after_report)
    typer.echo(json.dumps(diff, indent=2))
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(diff, indent=2), encoding="utf-8")
        typer.echo(f"\nwritten to {out}")


@app.command("prompt-compare")
def prompt_compare(
    dataset: str = typer.Option("eval/dataset.yaml"),
    sets: str = typer.Option("v1,v2", help="Comma-separated prompt sets to compare"),
    outdir: str = typer.Option("eval/reports"),
    judge: bool = typer.Option(False),
    allow_stub: bool = typer.Option(False),
) -> None:
    """Run the same evaluation set under two prompt sets and report the diff (L2)."""
    from .config import reload_settings
    from .eval.harness import compare_reports, run_evaluation
    from .prompts import reset_registry

    names = [s.strip() for s in sets.split(",") if s.strip()]
    if len(names) != 2:
        typer.echo("give exactly two prompt sets, e.g. --sets v1,v2")
        raise typer.Exit(code=2)

    from .config import get_settings as _settings

    if _settings().llm_provider == "stub":
        # The stub reads only the [[STEP:...]] tag and the clause ids in the
        # context block; it is blind to everything else in the prompt. A
        # comparison against it returns a zero delta on every metric, which
        # looks like "the change had no effect" and is in fact "nothing read
        # the change". Say so rather than printing the zeros.
        typer.echo(
            "\n  !! LLM_PROVIDER=stub.\n"
            "     The stub does not read prompt text -- only the step tag -- so every\n"
            "     metric will come back identical for both sets. That is not a result.\n"
            "     Configure a real provider before drawing any L2 conclusion.\n"
        )
        if not allow_stub:
            raise typer.Exit(code=2)

    reports = {}
    for name in names:
        os.environ["PROMPT_SET"] = name
        reset_registry()
        reload_settings()
        typer.echo(f"\n>>> evaluating prompt set {name}")
        report = run_evaluation(
            dataset_path=dataset,
            out_path=f"{outdir}/prompts_{name}.json",
            judge=judge,
            allow_stub=allow_stub,
            label=f"prompt set {name}",
        )
        _print_report(report)
        reports[name] = report

    diff = compare_reports(reports[names[0]], reports[names[1]])
    Path(outdir).mkdir(parents=True, exist_ok=True)
    Path(f"{outdir}/prompt_comparison.json").write_text(
        json.dumps(diff, indent=2), encoding="utf-8"
    )
    typer.echo("\n" + "=" * 78)
    typer.echo(f"  {names[0]} -> {names[1]}")
    typer.echo("=" * 78)
    for key, value in diff["metrics"].items():
        arrow = "better" if value["improved"] else "worse/flat"
        typer.echo(
            f"  {key:22s} {value['before']:.4f} -> {value['after']:.4f}  "
            f"({value['delta']:+.4f}, {arrow})"
        )
    typer.echo(
        f"  {'pass_rate':22s} {diff['pass_rate']['before']:.4f} -> "
        f"{diff['pass_rate']['after']:.4f} ({diff['pass_rate']['delta']:+.4f})"
    )
    if diff["fixed"]:
        typer.echo(f"\n  fixed: {[f['id'] for f in diff['fixed']]}")
    if diff["regressions"]:
        typer.echo(f"  regressed: {[r['id'] for r in diff['regressions']]}")
    typer.echo(f"\nwritten to {outdir}/prompt_comparison.json")


# -----------------------------------------------------------------------------
# Contracts and one-shot triage
# -----------------------------------------------------------------------------


@app.command("export-contracts")
def export_contracts(outdir: str = typer.Option("contracts")) -> None:
    """Write the JSON Schema for every published contract (D1)."""
    from .contracts import CONTRACT_VERSION
    from .contracts.models import FeedbackRequest, FNOLRequest, QuarantineRecord, TriageResult

    models = {
        "fnol": FNOLRequest,
        "triage_result": TriageResult,
        "feedback": FeedbackRequest,
        "quarantine": QuarantineRecord,
    }
    destination = Path(outdir)
    destination.mkdir(parents=True, exist_ok=True)
    for name, model in models.items():
        schema = model.model_json_schema()
        schema["$id"] = f"https://claim-triage.local/contracts/{name}.v{CONTRACT_VERSION}.json"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        path = destination / f"{name}.v1.schema.json"
        path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        typer.echo(f"  wrote {path}")


@app.command("triage")
def triage(
    path: str = typer.Argument(..., help="A JSON file containing one FNOL"),
    out: Optional[str] = typer.Option(None),
) -> None:
    """Validate and triage one FNOL in this process. No service required."""
    from .agents.runner import execute_run
    from .correlation import new_correlation_id, new_run_id
    from .guardrails import check_fnol
    from .validation import validate_submission

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    correlation_id = new_correlation_id()

    outcome = validate_submission(payload, correlation_id=correlation_id, client_id="cli")
    if not outcome.ok:
        typer.echo("REJECTED at the ingestion boundary. No agent run started.\n")
        typer.echo(json.dumps(outcome.quarantine.model_dump(mode="json"), indent=2, default=str))
        raise typer.Exit(code=1)

    guard = check_fnol(payload)
    if not guard.allowed:
        typer.echo("REJECTED by guardrails. No agent run started.\n")
        typer.echo(json.dumps([f.to_dict() for f in guard.findings], indent=2))
        raise typer.Exit(code=1)

    for warning in outcome.warnings:
        typer.echo(f"  warning  {warning.rule}: {warning.message}")

    result = execute_run(
        run_id=new_run_id(),
        correlation_id=correlation_id,
        claim=outcome.fnol.model_dump(mode="json"),  # type: ignore[union-attr]
        warnings=[w.model_dump(mode="json") for w in outcome.warnings],
        use_cache=False,
    )
    payload_out = result.model_dump(mode="json")
    if out:
        Path(out).write_text(json.dumps(payload_out, indent=2, default=str), encoding="utf-8")

    typer.echo(f"\nstatus     {result.status.value}")
    if result.coverage:
        typer.echo(f"position   {result.coverage.position.value}")
        typer.echo(f"summary    {result.coverage.summary}")
    if result.routing:
        typer.echo(f"queue      {result.routing.queue.value}  ({result.routing.reason[:120]})")
    typer.echo(
        f"verified   {result.verification.verdict} "
        f"(support {result.verification.support_score}, "
        f"{result.verification.citations_resolved}/{result.verification.citations_checked} citations)"
    )
    if result.escalation.required:
        typer.echo(f"escalated  {result.escalation.reason}: {result.escalation.explanation}")
    typer.echo("\ncitations")
    for citation in result.citations:
        typer.echo(f"  - {citation.section_path} (p{citation.page}, {citation.role})")
        typer.echo(f"    \"{citation.quote[:160]}\"")
    typer.echo(
        f"\nsteps {len(result.steps)}  tool calls {result.tool_calls}  "
        f"tokens {result.cost.total_tokens}  cost USD {result.cost.cost_usd}  "
        f"{result.duration_seconds}s"
    )


if __name__ == "__main__":
    app()
