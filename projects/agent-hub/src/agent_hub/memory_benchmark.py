"""Measure context support and complete response cost; never invent model answers or billing."""
import hashlib
import json
import math
import statistics
import time

from .memory_budget import Counter, serialized

STRATEGIES = ("full", "keyword", "hybrid", "layered")


def evaluate(cases, retrieve, *, counter=None):
    """retrieve(strategy, case) must execute the native implementation, not a recorded ranking."""
    if not 20 <= len(cases) <= 30 or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Use 20–30 distinct representative questions")
    counter = counter or Counter()
    if counter.method != "cl100k_base":
        raise ValueError("The pinned reference tokenizer is required for comparable results")
    rows = []
    for case in cases:
        for strategy in STRATEGIES:
            start = time.perf_counter()
            result = retrieve(strategy, case)
            elapsed = time.perf_counter() - start - result.get("metrics", {}).get("repeat_elapsed_ms", 0) / 1000
            payload = result["payload"]
            # Score source-bearing content only. Diagnostics/echoed queries cannot satisfy an expectation.
            context = "\n".join(result["context_texts"])
            missing = [value for value in case.get("expected", []) if value not in context]
            forbidden = [value for value in case.get("forbidden", []) if value in context]
            unauthorized = [value for value in case.get("unauthorized", []) if value in context]
            response_units = counter.count(payload)
            request_units = counter.count({"query": case["query"], "strategy": strategy})
            row = {
                "case": case["id"], "category": case["category"], "strategy": strategy,
                "expected_facts": len(case.get("expected", [])), "missing_facts": len(missing),
                "context_support_pass": not missing and not forbidden and not unauthorized,
                "stale_or_conflicted_exposures": len(forbidden), "unauthorized_exposures": len(unauthorized),
                "critical_rules_missed": len(missing) if case.get("critical") else 0,
                "request_reference_tokens": request_units, "response_reference_tokens": response_units,
                "complete_input_reference_tokens": request_units + response_units,
                "serialized_response_bytes": len(serialized(payload).encode()),
                "elapsed_ms": round(elapsed * 1000, 3), **result.get("metrics", {}),
            }
            if "repeat_payload" in result:
                row["repeat_response_reference_tokens"] = counter.count(result["repeat_payload"])
                row["repeat_unchanged"] = result["repeat_payload"].get("unchanged", 0)
            rows.append(row)
    summaries = {}
    for strategy in STRATEGIES:
        selected = [row for row in rows if row["strategy"] == strategy]
        expected = sum(row["expected_facts"] for row in selected)
        durations = sorted(row["elapsed_ms"] for row in selected)
        summaries[strategy] = {
            "cases": len(selected), "context_support_passed": sum(row["context_support_pass"] for row in selected),
            "expected_facts": expected, "missing_facts": sum(row["missing_facts"] for row in selected),
            "fact_context_recall": round(1 - sum(row["missing_facts"] for row in selected) / expected, 4) if expected else None,
            "critical_rules_missed": sum(row["critical_rules_missed"] for row in selected),
            "stale_or_conflicted_exposures": sum(row["stale_or_conflicted_exposures"] for row in selected),
            "unauthorized_exposures": sum(row["unauthorized_exposures"] for row in selected),
            "total_reference_tokens": sum(row["complete_input_reference_tokens"] for row in selected),
            "response_reference_tokens": sum(row["response_reference_tokens"] for row in selected),
            "median_ms": round(statistics.median(durations), 3), "p95_ms": durations[math.ceil(len(durations) * .95) - 1],
            "sql_queries": sum(row.get("sql_queries", 0) for row in selected),
            "embedding_requests": sum(row.get("embedding_requests", 0) for row in selected),
            "retries": sum(row.get("retries", 0) for row in selected),
        }
    return {"schema_version": 1, "cases_sha256": hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "tokenizer": counter.method, "measurement": "retrieved-context-support-and-complete-response-reference-tokens",
            "reference_input_scope": "benchmark-query-wrapper-plus-complete-returned-context; excludes-agent-system-prompt-and-host-tool-framing",
            "latency_scope": "initial-retrieval-only; repeat-latency-and-queries-reported-separately",
            "answer_correctness": "not-measured-no-completion-model", "provider_usage": "not-measured",
            "billing": "not-measured", "hindsight": "not-executed", "summary": summaries, "cases": rows}
