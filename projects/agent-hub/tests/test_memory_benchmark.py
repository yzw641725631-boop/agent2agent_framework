import pytest

from agent_hub.memory_benchmark import evaluate
from agent_hub.memory_budget import Counter


def test_query_echo_cannot_satisfy_context_support_and_complete_metadata_counts_toward_cost():
    cases = [{"id":str(i),"category":"critical","query":"必须核验来源","expected":["必须核验来源"],"critical":True} for i in range(20)]
    counter = Counter()
    def retrieve(strategy, case):
        return {"payload":{"query":case["query"],"items":[],"metadata":"diagnostic"*100},"context_texts":[]}
    report = evaluate(cases,retrieve,counter=counter)
    assert len(report["cases"]) == 80
    assert all(row["critical_rules_missed"] == 1 and not row["context_support_pass"] for row in report["cases"])
    assert all(row["response_reference_tokens"] > counter.count({"items":[]}) for row in report["cases"])
    assert report["billing"] == "not-measured" and report["answer_correctness"] == "not-measured-no-completion-model"


def test_benchmark_rejects_too_few_questions_and_duplicate_case_ids():
    for cases in [[{"id":str(i)} for i in range(19)], [{"id":"duplicate"}]*20]:
        with pytest.raises(ValueError):
            evaluate(cases,lambda *args: None)
