"""
Critical Evaluation Test Suite for Cross-Doc RAG with qwen2.5:7b

Tests simple, real-world user questions and evaluates each answer on:
  1. COMPLETENESS  — Does the answer contain ALL necessary steps/info?
  2. PROCEDURAL    — Can someone actually follow these steps and DO the thing?
  3. CROSS-DOC     — Did it pull info from multiple docs where needed?
  4. ACCURACY      — Are the concrete details (paths, commands, configs) correct?
  5. ACTIONABILITY — Could a downstream agent/automation act on this output?

Scoring: Each criterion 1-5. Output includes raw answer + critical judgment.
"""

import os
import sys
import json
import time
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "PageIndex"))

from cross_doc_rag import (
    MultiDocPageIndex,
    retrieve_with_cross_refs,
    synthesize_answer,
    query_pipeline,
)
import litellm

# ── Test Questions ──────────────────────────────────────────────────────────
# These simulate REAL user behavior: short, vague, procedural, sometimes sloppy

TEST_QUESTIONS = [
    # --- SIMPLE / VAGUE (mimicking lazy/casual users) ---
    {
        "id": "Q1",
        "question": "how do I deploy to production?",
        "category": "simple-vague",
        "expected_docs": ["jenkins-pipeline.md", "terraform-modules.md", "vault-secrets.md"],
        "must_contain": [
            "jenkins",
            "pipeline",
            "terraform",
            "apply",
            "vault",
        ],
        "description": "A typical developer question. Answer must cover the full deployment flow: Terraform apply, Docker build, Flyway migrations, K8s deploy — all from jenkins-pipeline.md but referencing other docs for details.",
    },
    {
        "id": "Q2",
        "question": "where are the database passwords stored?",
        "category": "simple-lookup",
        "expected_docs": ["vault-secrets.md", "azure-infrastructure.md"],
        "must_contain": [
            "vault",
            "sql_admin|secret/azure-sql|azure-sql",
            "key vault",
        ],
        "description": "Simple lookup. Must give EXACT Vault paths, explain how they flow to Azure Key Vault and into the application.",
    },
    {
        "id": "Q3",
        "question": "how to add a new database table",
        "category": "procedural",
        "expected_docs": ["flyway-migrations.md", "vault-secrets.md", "jenkins-pipeline.md"],
        "must_contain": [
            "flyway",
            "migration",
            "V",  # Flyway versioned migration naming like V003__
            "sql",
        ],
        "description": "Procedural task. Must explain: create migration file with correct naming, connection config, how to run it (locally + via Jenkins).",
    },

    # --- CROSS-CUTTING (requires linking multiple docs) ---
    {
        "id": "Q4",
        "question": "I need to set up a brand new environment from scratch, what's the full process?",
        "category": "cross-cutting",
        "expected_docs": ["terraform-modules.md", "azure-infrastructure.md", "vault-secrets.md", "flyway-migrations.md", "jenkins-pipeline.md"],
        "must_contain": [
            "terraform",
            "resource group",
            "vault",
            "flyway",
            "aks",
            "jenkins",
        ],
        "description": "The ultimate cross-doc question. Full answer needs: 1) Terraform for infra, 2) Vault for secrets, 3) Flyway for DB, 4) Docker + AKS deploy, 5) Jenkins pipeline to orchestrate. This MUST pull from all 5 docs.",
    },
    {
        "id": "Q5",
        "question": "what's the networking setup?",
        "category": "simple-lookup",
        "expected_docs": ["terraform-modules.md", "azure-infrastructure.md"],
        "must_contain": [
            "vnet",
            "subnet",
        ],
        "description": "Focused infrastructure question. Should return VNet config, subnet layout, NSG rules from terraform modules.",
    },

    # --- PROCEDURAL / TROUBLESHOOTING ---
    {
        "id": "Q6",
        "question": "flyway migration failed in CI, what do I check?",
        "category": "troubleshooting",
        "expected_docs": ["flyway-migrations.md", "jenkins-pipeline.md", "vault-secrets.md"],
        "must_contain": [
            "flyway",
            "connection",
            "vault",
            "jenkins",
        ],
        "description": "Troubleshooting scenario. Must cover: check Jenkins logs, verify Vault credentials, check DB connectivity, review migration file syntax, rollback options.",
    },
    {
        "id": "Q7",
        "question": "how does the app get secrets at runtime in kubernetes?",
        "category": "cross-cutting",
        "expected_docs": ["vault-secrets.md", "azure-infrastructure.md", "terraform-modules.md"],
        "must_contain": [
            "vault",
            "csi",
            "key vault",
            "kubernetes",
        ],
        "description": "Must trace: Vault → Azure Key Vault sync → CSI Secret Store driver → Kubernetes pod volume mount. Spans vault-secrets.md and azure-infrastructure.md.",
    },

    # --- EDGE CASE / SCALE TEST ---
    {
        "id": "Q8",
        "question": "tell me about monitoring and alerts",
        "category": "simple-vague",
        "expected_docs": ["azure-infrastructure.md", "terraform-modules.md"],
        "must_contain": [
            "monitor",
            "alert",
            "log_analytics|log analytics",
        ],
        "description": "Vague monitoring question. Should cover Azure Monitor, Log Analytics, alert rules, Application Insights.",
    },
    {
        "id": "Q9",
        "question": "how to rotate the sql database admin password without downtime",
        "category": "procedural-complex",
        "expected_docs": ["vault-secrets.md", "jenkins-pipeline.md", "azure-infrastructure.md", "flyway-migrations.md"],
        "must_contain": [
            "vault",
            "rotation",
            "credential",
        ],
        "description": "Complex procedural question requiring: update in Vault, sync to Azure Key Vault, restart/rolling update in AKS, verify Flyway can still connect.",
    },
    {
        "id": "Q10",
        "question": "what terraform modules do we have and what does each one do?",
        "category": "inventory",
        "expected_docs": ["terraform-modules.md"],
        "must_contain": [
            "resource_group",
            "networking",
            "aks",
            "acr",
            "key_vault",
            "sql_database",
        ],
        "description": "Inventory question. Must list ALL terraform modules with brief description of each. Tests whether the system can return a comprehensive listing.",
    },
]

# ── Evaluation ──────────────────────────────────────────────────────────────

def evaluate_answer(question_data: dict, answer: str, sources: list[dict]) -> dict:
    """Critically evaluate a RAG answer on 5 dimensions."""

    q = question_data
    answer_lower = answer.lower()

    # 1. MUST-CONTAIN check (supports | alternation: "term1|term2" matches either)
    found_terms = []
    missing_terms = []
    for term in q["must_contain"]:
        alternatives = [t.strip().lower() for t in term.split("|")]
        if any(alt in answer_lower for alt in alternatives):
            found_terms.append(term)
        else:
            missing_terms.append(term)
    must_contain_score = len(found_terms) / len(q["must_contain"]) if q["must_contain"] else 1.0

    # 2. CROSS-DOC check — which expected docs were actually sourced
    source_files = set(s["filename"] for s in sources)
    expected = set(q["expected_docs"])
    docs_hit = source_files & expected
    docs_missed = expected - source_files
    cross_doc_score = len(docs_hit) / len(expected) if expected else 1.0

    # 3. PROCEDURAL check — look for step-like patterns
    procedural_indicators = [
        answer_lower.count("step "),
        answer_lower.count("1.") + answer_lower.count("2.") + answer_lower.count("3."),
        answer_lower.count("first"),
        answer_lower.count("then"),
        answer_lower.count("next"),
        answer_lower.count("finally"),
        answer_lower.count("run "),
        answer_lower.count("execute"),
        answer_lower.count("configure"),
        answer_lower.count("create"),
    ]
    procedural_score = min(sum(procedural_indicators) / 5.0, 1.0)

    # 4. SPECIFICITY check — concrete values vs vague statements
    specificity_indicators = [
        answer.count("`"),       # inline code
        answer.count("```"),     # code blocks
        answer.count("/"),       # paths
        answer.count("="),       # config assignments
        answer.count("--"),      # CLI flags
        len([l for l in answer.splitlines() if l.strip().startswith("-")]),  # bullet points
    ]
    specificity_score = min(sum(specificity_indicators) / 15.0, 1.0)

    # 5. LENGTH adequacy
    word_count = len(answer.split())
    if q["category"] in ("cross-cutting", "procedural-complex"):
        length_score = min(word_count / 200.0, 1.0)  # Need at least ~200 words
    elif q["category"] in ("simple-lookup", "inventory"):
        length_score = min(word_count / 80.0, 1.0)
    else:
        length_score = min(word_count / 120.0, 1.0)

    # Aggregate
    scores = {
        "must_contain": round(must_contain_score * 5, 1),
        "cross_doc": round(cross_doc_score * 5, 1),
        "procedural": round(procedural_score * 5, 1),
        "specificity": round(specificity_score * 5, 1),
        "length_adequacy": round(length_score * 5, 1),
    }
    scores["overall"] = round(sum(scores.values()) / 5, 1)

    verdict_parts = []
    if missing_terms:
        verdict_parts.append(f"MISSING terms: {missing_terms}")
    if docs_missed:
        verdict_parts.append(f"MISSED docs: {list(docs_missed)}")
    if word_count < 50:
        verdict_parts.append(f"TOO SHORT ({word_count} words)")
    if procedural_score < 0.3 and q["category"] in ("procedural", "procedural-complex", "cross-cutting"):
        verdict_parts.append("NOT PROCEDURAL enough — lacks step-by-step structure")
    if specificity_score < 0.3:
        verdict_parts.append("TOO VAGUE — lacks concrete commands/paths/configs")

    return {
        "scores": scores,
        "found_terms": found_terms,
        "missing_terms": missing_terms,
        "docs_hit": list(docs_hit),
        "docs_missed": list(docs_missed),
        "source_files": list(source_files),
        "word_count": word_count,
        "issues": verdict_parts,
        "pass": scores["overall"] >= 3.0 and not missing_terms,
    }


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    DOCS_DIR = Path(__file__).parent / "docs"
    WORKSPACE = Path(__file__).parent / "workspace"
    MODEL = "ollama/qwen2.5:7b"

    os.environ.setdefault("OPENAI_API_KEY", "ollama")

    print(f"{'='*70}")
    print(f"  CRITICAL EVALUATION SUITE — Model: {MODEL}")
    print(f"  {len(TEST_QUESTIONS)} test questions across categories")
    print(f"{'='*70}\n")

    # Index
    index = MultiDocPageIndex(docs_dir=str(DOCS_DIR), workspace=str(WORKSPACE), model=MODEL)
    index.index_all()

    results = []

    for i, q in enumerate(TEST_QUESTIONS, 1):
        print(f"\n{'#'*70}")
        print(f"  TEST {q['id']} [{q['category']}]: {q['question']}")
        print(f"  Expected docs: {q['expected_docs']}")
        print(f"{'#'*70}")

        start = time.time()

        # Run retrieval
        context_parts = retrieve_with_cross_refs(index, q["question"], verbose=True)

        # Run synthesis
        answer = synthesize_answer(index, q["question"], context_parts)
        elapsed = time.time() - start

        # Print answer
        print(f"\n{'='*70}")
        print(f"ANSWER ({len(answer.split())} words, {elapsed:.1f}s):")
        print(f"{'='*70}")
        for line in answer.splitlines():
            print(textwrap.fill(line, width=100))

        # Evaluate
        evaluation = evaluate_answer(q, answer, context_parts)
        evaluation["elapsed_sec"] = round(elapsed, 1)
        evaluation["question"] = q["question"]
        evaluation["id"] = q["id"]
        evaluation["category"] = q["category"]

        # Print evaluation
        print(f"\n{'─'*70}")
        print(f"  EVALUATION for {q['id']}:")
        print(f"{'─'*70}")
        s = evaluation["scores"]
        print(f"  Must-Contain:    {s['must_contain']}/5  (found: {evaluation['found_terms']})")
        if evaluation["missing_terms"]:
            print(f"                   !! MISSING: {evaluation['missing_terms']}")
        print(f"  Cross-Doc:       {s['cross_doc']}/5  (hit: {evaluation['docs_hit']})")
        if evaluation["docs_missed"]:
            print(f"                   !! MISSED: {evaluation['docs_missed']}")
        print(f"  Procedural:      {s['procedural']}/5")
        print(f"  Specificity:     {s['specificity']}/5")
        print(f"  Length:          {s['length_adequacy']}/5  ({evaluation['word_count']} words)")
        print(f"  ─────────────────────────")
        print(f"  OVERALL:         {s['overall']}/5  {'✓ PASS' if evaluation['pass'] else '✗ FAIL'}")
        if evaluation["issues"]:
            print(f"  ISSUES:")
            for issue in evaluation["issues"]:
                print(f"    ⚠  {issue}")
        print(f"  Time: {evaluation['elapsed_sec']}s")

        results.append(evaluation)

    # ── Final Summary ───────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print(f"  FINAL SCORECARD — {MODEL}")
    print(f"{'='*70}\n")

    print(f"{'ID':<5} {'Category':<22} {'Overall':>8} {'Must':>6} {'XDoc':>6} {'Proc':>6} {'Spec':>6} {'Len':>6} {'Time':>6} {'Pass':>6}")
    print(f"{'─'*75}")

    pass_count = 0
    total_overall = 0
    for r in results:
        s = r["scores"]
        status = "✓" if r["pass"] else "✗"
        print(f"{r['id']:<5} {r['category']:<22} {s['overall']:>7.1f} {s['must_contain']:>5.1f} {s['cross_doc']:>5.1f} {s['procedural']:>5.1f} {s['specificity']:>5.1f} {s['length_adequacy']:>5.1f} {r['elapsed_sec']:>5.1f}s {status:>5}")
        total_overall += s["overall"]
        if r["pass"]:
            pass_count += 1

    avg = total_overall / len(results)
    print(f"{'─'*75}")
    print(f"{'AVG':<28} {avg:>7.1f}")
    print(f"\nPassed: {pass_count}/{len(results)} ({100*pass_count/len(results):.0f}%)")

    # Category breakdown
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r["scores"]["overall"])
    print(f"\nBy Category:")
    for cat, scores in sorted(categories.items()):
        avg_cat = sum(scores) / len(scores)
        print(f"  {cat:<25} avg: {avg_cat:.1f}/5  ({len(scores)} questions)")

    # Worst failures
    failures = [r for r in results if not r["pass"]]
    if failures:
        print(f"\nFAILED QUESTIONS ({len(failures)}):")
        for r in failures:
            print(f"  {r['id']}: {r['question']}")
            for issue in r["issues"]:
                print(f"        ⚠  {issue}")

    # Write full results to file
    output_path = Path(__file__).parent / "eval_results_7b.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull results written to: {output_path}")


if __name__ == "__main__":
    main()
