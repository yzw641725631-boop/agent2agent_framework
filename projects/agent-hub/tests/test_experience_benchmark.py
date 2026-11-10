import json
import uuid

import httpx
import pytest

from agent_hub.experience_benchmark import load, run
from agent_hub.hindsight_engine import HindsightEngine
from agent_hub.security import HubError


def test_frozen_comparison_checks_readiness_uses_same_sources_and_omits_raw_queries(tmp_path):
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    value = {
        "schema_version": 1,
        "source_class": "synthetic",
        "project_id": str(uuid.uuid4()),
        "repository_id": str(uuid.uuid4()),
        "records": [
            {"id": first, "revision": "a" * 64, "text": "受限的中文登录经验"},
            {"id": second, "revision": "b" * 64, "text": "另一个受众的经验"},
        ],
        "cases": [
            {
                "query": "原始问题不应出现在报告中",
                "allowed_ids": [first],
                "expected_ids": [first],
                "native_ids": [first],
            }
        ],
    }
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(value), encoding="utf-8")

    def serve(request):
        if request.method == "GET":
            return httpx.Response(200, json={"status": "completed"})
        body = json.loads(request.content)
        assert body["tags"] == ["hub-source:" + first + ":" + "a" * 64]
        return httpx.Response(200, json={"results": [{"tags": body["tags"], "text": "untrusted"}]})

    def factory(bank, **kwargs):
        return HindsightEngine(bank, transport=httpx.MockTransport(serve))

    report = run(fixture, operation_id=str(uuid.uuid4()), engine_factory=factory)
    assert report["cases"][0]["native"]["hits"] == report["cases"][0]["hindsight"]["hits"] == 1
    assert not report["production_engine_changed"]
    assert "原始问题" not in json.dumps(report, ensure_ascii=False)
    value["cases"][0]["expected_ids"] = [second]
    fixture.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HubError, match="EXPERIENCE_FIXTURE_SCOPE_INVALID"):
        load(fixture)
