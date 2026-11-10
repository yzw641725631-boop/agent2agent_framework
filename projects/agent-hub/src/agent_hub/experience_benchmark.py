"""Operator-only, frozen-source comparison. No production identity or source database is accessed."""

import hashlib
import json
import time
import uuid
from pathlib import Path

from .hindsight_engine import Candidate, HindsightEngine, bank_id, comparison
from .security import HubError


def load(filename):
    raw = Path(filename).read_bytes()
    if len(raw) > 262144:
        raise HubError("EXPERIENCE_FIXTURE_TOO_LARGE", 422)
    value = json.loads(raw)
    if value.get("schema_version") != 1 or value.get("source_class") not in {
        "synthetic",
        "authorized-export",
    }:
        raise HubError("EXPERIENCE_FIXTURE_INVALID", 422)
    records = [
        Candidate(str(uuid.UUID(item["id"])), item["revision"], item["text"]) for item in value["records"]
    ]
    HindsightEngine.check(records)
    cases = value["cases"]
    if not 1 <= len(cases) <= 32:
        raise HubError("EXPERIENCE_FIXTURE_INVALID", 422)
    ids = {r.id for r in records}
    for case in cases:
        allowed = set(case["allowed_ids"])
        if (
            not allowed <= ids
            or not set(case["expected_ids"]) <= allowed
            or not set(case.get("native_ids", [])) <= allowed
            or not isinstance(case["query"], str)
            or not 1 <= len(case["query"]) <= 200
        ):
            raise HubError("EXPERIENCE_FIXTURE_SCOPE_INVALID", 422)
    audience = hashlib.sha256(
        json.dumps([c["allowed_ids"] for c in cases], sort_keys=True).encode()
    ).hexdigest()
    corpus = hashlib.sha256(raw).hexdigest()
    bank = bank_id(value["project_id"], value["repository_id"], audience, corpus)
    return value, records, bank, corpus


def run(
    filename, *, api_key="", ingest=False, allow_model_calls=False, operation_id=None, engine_factory=None
):
    value, records, bank, corpus = load(filename)
    engine = (engine_factory or HindsightEngine)(bank, api_key=api_key)
    try:
        report = {
            "schema_version": 1,
            "source_class": value["source_class"],
            "corpus_sha256": corpus,
            "bank": bank,
            "automatic_retry": False,
            "production_engine_changed": False,
        }
        if ingest:
            report.update(
                state="indexing-pending",
                ingestion=engine.retain(records, allow_model_calls=allow_model_calls),
            )
            return report
        if not operation_id:
            raise HubError("HINDSIGHT_COMPLETED_OPERATION_REQUIRED", 409)
        engine.ready(operation_id)
        report.update(
            state="evaluated",
            cases=[],
            native_baseline_trust="fixture-reported-order",
            provider_usage="unknown",
            billing_comparison="not-measured",
        )
        for number, case in enumerate(value["cases"], 1):
            allowed = set(case["allowed_ids"])
            candidates = [r for r in records if r.id in allowed]
            start = time.monotonic()
            ranked = engine.rank(case["query"], candidates) if candidates else {"ordered_ids": []}
            result = {
                "case": number,
                "allowed": len(allowed),
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "query_sha256": hashlib.sha256(case["query"].encode()).hexdigest(),
                **comparison(case.get("native_ids", []), ranked, case["expected_ids"]),
            }
            if "native_ids" not in case:
                result["native"] = {"state": "baseline-not-provided"}
            report["cases"].append(result)
        return report
    finally:
        engine.close()
