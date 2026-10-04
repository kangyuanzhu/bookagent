from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bookagent.models import Page
from bookagent.summarize import (
    OpenAISummarizer, PageSummary, SummaryBatch, extractive_pages,
    summarize_overview, validate_result, visible_text, word_count,
)


def source_page(number=3):
    return Page(number, "", 0, 60, "", [], "", source_text="[00:00:05] The controller checks progress before another action. Failures require a retry or a changed plan.")


def summary(number=3):
    return PageSummary(page_id=number, title="Check progress", summary="The controller checks progress before acting.",
                       bullets=["Retry or replan after failure."], takeaway="Check before continuing.",
                       chapter="Control", uncertainty="", evidence=["The controller checks progress"])


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.responses = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.outputs.pop(0))


def test_checkpoint_reuse_and_budget_invalidation(tmp_path):
    client = FakeClient([SummaryBatch(pages=[summary()]), SummaryBatch(pages=[summary()])])
    agent = OpenAISummarizer("test-model", tmp_path, client=client)
    page = source_page()
    first = agent.summarize([page], 40)
    assert first[0].summary == "The controller checks progress before acting."
    assert page.summary == ""
    assert len(client.calls) == 1
    assert client.calls[0]["store"] is False
    assert client.calls[0]["text_format"] is SummaryBatch
    assert agent.summarize([page], 40) == first
    assert len(client.calls) == 1
    agent.summarize([page], 41)
    assert len(client.calls) == 2


def test_invalid_quote_is_rejected_and_retried(tmp_path):
    bad = summary().model_copy(update={"evidence": ["Progress checks guarantee success"]})
    client = FakeClient([SummaryBatch(pages=[bad]), SummaryBatch(pages=[summary()])])
    result = OpenAISummarizer("test", tmp_path, client=client).summarize([source_page()], 40)
    assert result[0].title == "Check progress"
    assert len(client.calls) == 2
    assert "absent from the transcript" in client.calls[1]["instructions"]


def test_missing_pages_fail_validation():
    with pytest.raises(ValueError, match="page IDs"):
        validate_result(SummaryBatch(pages=[summary(4)]), [source_page(3)], 40)


def test_budget_does_not_count_hidden_evidence():
    with pytest.raises(ValueError, match="budget"):
        validate_result(SummaryBatch(pages=[summary()]), [source_page()], 10)
    validate_result(SummaryBatch(pages=[summary()]), [source_page()], 40)


def test_refusal_is_not_converted_to_empty_pages(tmp_path):
    client = FakeClient([None])
    with pytest.raises(RuntimeError, match="no parsed summary"):
        OpenAISummarizer("test", tmp_path, client=client).summarize([source_page()], 40)
    assert not list(tmp_path.glob("*.json"))


def test_corrupt_cache_regenerates(tmp_path):
    client = FakeClient([SummaryBatch(pages=[summary()]), SummaryBatch(pages=[summary()])])
    agent = OpenAISummarizer("test", tmp_path, client=client)
    agent.summarize([source_page()], 40)
    next(tmp_path.glob("*.json")).write_text("not json")
    agent.summarize([source_page()], 40)
    assert len(client.calls) == 2


def test_extracts_are_labeled_and_keep_full_source():
    page = source_page()
    result = extractive_pages([page], 18)[0]
    assert "no AI summary" in result.uncertainty
    assert result.source_text == page.source_text
    assert result.summary.endswith("…")
    assert word_count(visible_text(result)) <= 18


def test_overlarge_source_is_never_silently_truncated(tmp_path):
    client = FakeClient([])
    with pytest.raises(ValueError, match="no transcript was truncated"):
        OpenAISummarizer("test", tmp_path, client=client).summarize([replace(source_page(), source_text="x" * 60_001)], 40)
    assert not client.calls


def test_large_200_page_overview_reduces_in_bounded_stages(tmp_path):
    class SourceAwareClient:
        def __init__(self):
            self.responses = self
            self.requests = []

        def parse(self, **kwargs):
            import json
            import re
            records = json.loads(kwargs["input"][0]["content"][0]["text"])["pages"]
            self.requests.extend(records)
            output = []
            for record in records:
                quote = " ".join(re.sub(r"\[\d+:\d{2}:\d{2}\]", "", record["transcript"]).split()[:5])
                output.append(summary(record["page_id"]).model_copy(update={"evidence": [quote]}))
            return SimpleNamespace(output_parsed=SummaryBatch(pages=output))

    pages = [replace(source_page(i + 3), summary="The controller checks progress before another action and retries failures. " * 8)
             for i in range(198)]
    client = SourceAwareClient()
    result = summarize_overview(OpenAISummarizer("test", tmp_path, client=client), pages, 14998)
    assert result.number == 2
    assert result.kind == "overview"
    assert result.chapter == "Reading guide"
    assert len(client.requests) > 1
    assert all(len(record["transcript"]) <= 50_000 for record in client.requests)
