"""Tests for the web-researched audit (chunk 4O).

`cli enrich --audit` researches each discovered maker waiting in the /review
queue with the API's web search tool and drafts a verdict ("a real, active
wristwatch maker?"), the brand a person / partial name belongs to, a
pre-launch flag, and every field on the card, with the pages it relied on.
Covers:
  1. lookup_maker_audit — run through the real pinned SDK against an
     in-process fake Messages API (offline): the request carries the web search
     tool and the answer tool; the answer is read from the answer tool's call,
     or failing that from a JSON object in the text, even narrated and split
     across text blocks; sources are limited to URLs the search returned;
     pause_turn, max_tokens, no answer or no valid verdict give None, cache
     nothing and log why; results are cached.
  2. Selection and verification — which makers are researched, in queue order;
     every drafted value is checked; a "no" drafts no fields.
  3. Propose — failures are isolated and retried; a systemic failure stops the
     run; the cache is committed as results arrive.
  4. Apply — fills blanks and replaces only "auto" values, independence rules,
     verdict + stamps, the pre-launch flag, and the in-flight guard (a maker
     reviewed mid-run is skipped) — also for the older factual / taste paths.
  5. The CLI — dry run writes nothing, --limit/--commit, the failure stop.
  6. The review card — verdict, reason, sources, brand suggestion (merge link
     or rename hint), "no" first, the pre-launch chip / checkbox / filter.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from watch_app import taste
from watch_app.api.routers.makers import (
    _fetch_discovered_for_review,
    router as makers_router,
)
from watch_app.cli import app as cli_app
from watch_app.config import get_settings
from watch_app.extraction import enrich
from watch_app.extraction.enrich import (
    MAX_CONSECUTIVE_AUDIT_FAILURES,
    apply_audits,
    apply_enrichments,
    apply_taste_proposals,
    iter_unaudited_makers,
    propose_audits,
    propose_enrichments,
    propose_taste_for_discovered,
)
from watch_app.llm import client as llm
from watch_app.storage.db import session_scope
from watch_app.storage.models import LLMCache, Maker, Score


DISC = "Zzq Audit Maker"
SEED_NAME = "Simon Brette"  # present in the seed YAML
HX = {"HX-Request": "true"}


@pytest.fixture
def client(test_engine):
    app = FastAPI()
    app.include_router(makers_router)
    return TestClient(app)


@pytest.fixture
def admin_on(monkeypatch):
    monkeypatch.setenv("WATCH_APP_ADMIN", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add(name: str, **kwargs) -> int:
    defaults = {"is_independent": True}
    defaults.update(kwargs)
    with session_scope() as session:
        m = Maker(canonical_name=name, **defaults)
        session.add(m)
        session.flush()
        return m.id


def _get(mid: int) -> Maker:
    with session_scope() as session:
        return session.get(Maker, mid)


class _Resolved:
    status_code = 200


# ---------- a fake Messages API, driven through the real SDK ----------


def _fake_api(monkeypatch, reply: dict) -> list[dict]:
    """Point the LLM client at an in-process fake Messages API that always
    answers `reply`, and return the list it records request bodies into. The
    pinned SDK builds the request and parses the response for real."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    fake = anthropic.Anthropic(
        api_key="sk-test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(llm, "_client", fake)
    return calls


def _reply(*blocks, stop_reason: str = "end_turn", searches: int = 2) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": llm.AUDIT_MODEL,
        "content": list(blocks),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 18000,
            "output_tokens": 400,
            "server_tool_use": {"web_search_requests": searches},
        },
    }


def _results(*urls: str) -> dict:
    return {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": [
            {
                "type": "web_search_result",
                "url": url,
                "title": f"Page {i}",
                "encrypted_content": "x",
                "page_age": None,
            }
            for i, url in enumerate(urls)
        ],
    }


def _text(body: str, cites: tuple[str, ...] = ()) -> dict:
    block: dict = {"type": "text", "text": body}
    if cites:
        block["citations"] = [
            {
                "type": "web_search_result_location",
                "url": url,
                "title": "cited",
                "encrypted_index": "y",
                "cited_text": "...",
            }
            for url in cites
        ]
    return block


ANSWER = {
    "verdict": "yes",
    "reason": "Norwegian microbrand selling a GMT direct.",
    "brand_name": None,
    "pre_launch": False,
    "country": "Norway",
    "founded_year": 2021,
    "website": "https://fjordwatch.no",
    "is_independent": True,
    "aesthetic_tags": ["tool"],
    "complication_focus": None,
    "maker_type": "microbrand",
    "price_band": "mid",
    "output_scale": None,
    "distribution": "direct",
    "movement_origin": "stock-base",
    "sources": ["https://fjordwatch.no", "https://invented.example/page"],
}


def _tool_answer_blocks(answer: dict = ANSWER) -> list[dict]:
    """The expected shape: a search, a line of cited prose, then the answer
    through the answer tool (the reply then stops with "tool_use")."""
    return [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "Fjord Watch Co"}},
        _results("https://fjordwatch.no/", "https://monochrome.example/fjord-gmt/"),
        _text("A Norwegian microbrand.", cites=("https://monochrome.example/fjord-gmt/",)),
        {"type": "tool_use", "id": "toolu_1", "name": llm.AUDIT_TOOL_NAME,
         "input": answer},
    ]


def _answer_blocks(answer: dict = ANSWER) -> list[dict]:
    """The fallback shape: narration, a search, then the answer written out as
    JSON, split across two text blocks (as citations can split it)."""
    body = json.dumps(answer)
    half = len(body) // 2
    return [
        _text("Let me look that up."),
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "Fjord Watch Co"}},
        _results("https://fjordwatch.no/", "https://monochrome.example/fjord-gmt/"),
        _text(body[:half], cites=("https://monochrome.example/fjord-gmt/",)),
        _text(body[half:]),
    ]


# ---------- 1. lookup_maker_audit ----------


def test_lookup_sends_web_search_and_answer_tools(session, monkeypatch):
    calls = _fake_api(monkeypatch, _reply(*_tool_answer_blocks(), stop_reason="tool_use"))
    llm.lookup_maker_audit(session, "Fjord Watch Co.", country="Norway")
    [body] = calls
    assert body["model"] == llm.AUDIT_MODEL
    assert body["max_tokens"] == llm.AUDIT_MAX_TOKENS
    search, answer_tool = body["tools"]
    assert search == {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
    assert answer_tool["name"] == llm.AUDIT_TOOL_NAME
    schema = answer_tool["input_schema"]
    assert schema["required"] == ["verdict", "reason"]
    assert schema["properties"]["verdict"]["enum"] == list(enrich.VERDICTS)
    # Every drafted field can be answered, lists where the vocabulary allows several.
    assert set(llm.AUDIT_FIELD_NAMES) <= set(schema["properties"])
    assert schema["properties"]["aesthetic_tags"]["type"] == "array"
    assert schema["properties"]["maker_type"]["type"] == "string"
    assert llm.AUDIT_TOOL_NAME in body["messages"][0]["content"]


def test_lookup_takes_the_answer_from_the_answer_tool(session, monkeypatch):
    _fake_api(monkeypatch, _reply(*_tool_answer_blocks(), stop_reason="tool_use"))
    res = llm.lookup_maker_audit(session, "Fjord Watch Co.")
    assert res["verdict"] == "yes"
    assert res["reason"].startswith("Norwegian microbrand")
    assert res["fields"]["website"] == "https://fjordwatch.no"
    assert res["fields"]["maker_type"] == "microbrand"
    assert "output_scale" not in res["fields"]  # nulls are dropped
    assert res["usage"]["web_search_requests"] == 2


def test_lookup_answer_tool_wins_over_json_in_the_text(session, monkeypatch):
    blocks = _answer_blocks({**ANSWER, "verdict": "unsure"})
    blocks.append({"type": "tool_use", "id": "toolu_1", "name": llm.AUDIT_TOOL_NAME,
                   "input": ANSWER})
    _fake_api(monkeypatch, _reply(*blocks, stop_reason="tool_use"))
    assert llm.lookup_maker_audit(session, "Fjord Watch Co.")["verdict"] == "yes"


def test_lookup_parses_a_narrated_answer_split_across_blocks(session, monkeypatch):
    """The fallback: a reply that writes its answer out as JSON instead of
    calling the answer tool is still read."""
    _fake_api(monkeypatch, _reply(*_answer_blocks()))
    res = llm.lookup_maker_audit(session, "Fjord Watch Co.")
    assert res["verdict"] == "yes"
    assert res["reason"].startswith("Norwegian microbrand")
    assert res["fields"]["country"] == "Norway"
    assert res["fields"]["founded_year"] == 2021
    assert "output_scale" not in res["fields"]  # nulls are dropped
    assert res["usage"] == {
        "input_tokens": 18000,
        "output_tokens": 400,
        "web_search_requests": 2,
    }
    assert res["cached"] is False


def test_lookup_keeps_only_sources_the_search_returned(session, monkeypatch):
    _fake_api(monkeypatch, _reply(*_tool_answer_blocks(), stop_reason="tool_use"))
    res = llm.lookup_maker_audit(session, "Fjord Watch Co.")
    urls = [s["url"] for s in res["sources"]]
    # The listed site matches its result despite the trailing slash; the
    # invented URL is dropped; the cited page tops the list up.
    assert urls == ["https://fjordwatch.no/", "https://monochrome.example/fjord-gmt/"]
    assert res["sources"][0]["title"] == "Page 0"


def test_lookup_keeps_one_source_per_site(session, monkeypatch):
    """A second page from a site already listed (with or without www.) gives
    way to the next site — the second live run listed yema.com twice."""
    pages = ("https://fjordwatch.no/", "https://www.fjordwatch.no/gmt",
             "https://FJORDWATCH.no/about", "https://monochrome.example/fjord-gmt/",
             "https://hodinkee.example/fjord")
    _fake_api(monkeypatch, _reply(
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "Fjord Watch Co"}},
        _results(*pages),
        {"type": "tool_use", "id": "toolu_1", "name": llm.AUDIT_TOOL_NAME,
         "input": {**ANSWER, "sources": list(pages)}},
        stop_reason="tool_use",
    ))
    res = llm.lookup_maker_audit(session, "Fjord Watch Co.")
    assert [s["url"] for s in res["sources"]] == [
        "https://fjordwatch.no/",
        "https://monochrome.example/fjord-gmt/",
        "https://hodinkee.example/fjord",
    ]


def test_lookup_pause_turn_returns_none_and_caches_nothing(session, monkeypatch):
    _fake_api(monkeypatch, _reply(*_tool_answer_blocks(), stop_reason="pause_turn"))
    assert llm.lookup_maker_audit(session, "Fjord Watch Co.") is None
    assert session.query(LLMCache).count() == 0


def test_lookup_cut_off_by_max_tokens_returns_none(session, monkeypatch, caplog):
    """An answer cut off part-way can't be trusted, even if what arrived
    looks complete."""
    _fake_api(monkeypatch, _reply(*_tool_answer_blocks(), stop_reason="max_tokens"))
    assert llm.lookup_maker_audit(session, "Fjord Watch Co.") is None
    assert session.query(LLMCache).count() == 0
    assert "max_tokens" in caplog.text


def test_lookup_without_an_answer_returns_none_and_says_why(session, monkeypatch, caplog):
    _fake_api(monkeypatch, _reply(
        _text("Fjord Watch Co. is a Norwegian microbrand " + "and more " * 60 + "THE END"),
    ))
    assert llm.lookup_maker_audit(session, "Fjord Watch Co.") is None
    assert session.query(LLMCache).count() == 0
    # The warning carries what's needed to tell why: how the reply stopped,
    # the searches, and how it ended (not just how it began).
    assert "stop_reason=end_turn" in caplog.text
    assert "2 search(es)" in caplog.text
    assert "THE END" in caplog.text


def test_lookup_without_a_valid_verdict_returns_none(session, monkeypatch, caplog):
    for answer in ({"reason": "no verdict given"}, {**ANSWER, "verdict": "maybe"}):
        _fake_api(monkeypatch, _reply(
            {"type": "tool_use", "id": "toolu_1", "name": llm.AUDIT_TOOL_NAME,
             "input": answer},
            stop_reason="tool_use",
        ))
        assert llm.lookup_maker_audit(session, "Fjord Watch Co.") is None
    assert session.query(LLMCache).count() == 0
    assert "verdict 'maybe'" in caplog.text


def test_lookup_drops_stand_ins_for_no_value(session, monkeypatch):
    """The second live run got the word "null" back as a brand name; that, and
    the like anywhere in the answer, means no value."""
    answer = {**ANSWER, "brand_name": "null", "country": " None ", "website": "",
              "founded_year": "unknown", "aesthetic_tags": ["NULL", "tool"],
              "complication_focus": ["n/a"]}
    _fake_api(monkeypatch, _reply(
        {"type": "tool_use", "id": "toolu_1", "name": llm.AUDIT_TOOL_NAME, "input": answer},
        stop_reason="tool_use",
    ))
    res = llm.lookup_maker_audit(session, "Fjord Watch Co.")
    assert res["verdict"] == "yes"
    assert res["brand_name"] is None
    for gone in ("country", "website", "founded_year", "complication_focus"):
        assert gone not in res["fields"]
    assert res["fields"]["aesthetic_tags"] == ["tool"]
    assert res["fields"]["maker_type"] == "microbrand"  # real values untouched


def test_audit_prompt_judges_the_name_as_the_mentions_use_it():
    """Split came back "yes" on the second live run: a brand of that name
    exists, though the mentions were about split-seconds chronographs."""
    with_mentions = llm._audit_prompt("Split", None, "- a split-seconds chronograph")
    assert "Judge the name as these mentions use it" in with_mentions
    assert "even if some brand of that name exists" in with_mentions
    # Without mentions there is nothing to judge the name against.
    assert "Judge the name" not in llm._audit_prompt("Split", None, None)
    # Absent values are left out, never written as null (see the test above).
    assert "null" not in with_mentions


def test_audit_prompt_asks_for_sources_on_every_verdict():
    """The second live run gave two "no" verdicts no sources: sources came
    after country in the field list, and a "no" leaves out "everything from
    country down"."""
    prompt = llm._audit_prompt("Split", None, None)
    assert "leave out everything from country down" in prompt
    assert prompt.index("- sources:") < prompt.index("- country:")
    assert "for every verdict, a \"no\" included" in prompt


def test_audit_prompt_suggests_a_brand_only_for_its_own_names():
    """A suggested brand becomes a merge link, and a merge makes the name an
    alias. The fourth live run suggested Petermann Bédat for "Split" (its
    mentions were about that brand's split-seconds watches), which would have
    credited every future split-seconds mention to Petermann Bédat."""
    prompt = " ".join(llm._audit_prompt("Split", None, None).split())
    assert "brand_name: only when the name belongs to a brand" in prompt
    assert "the name will count as that brand from now on" in prompt
    assert "Leave it out for a general term or phrase" in prompt
    assert "a designer another company's watch pays homage to" in prompt


def test_lookup_result_is_cached(session, monkeypatch):
    calls = _fake_api(monkeypatch, _reply(*_tool_answer_blocks(), stop_reason="tool_use"))
    llm.lookup_maker_audit(session, "Fjord Watch Co.", context="- a headline")
    again = llm.lookup_maker_audit(session, "Fjord Watch Co.", context="- a headline")
    assert len(calls) == 1
    assert again["cached"] is True
    assert again["verdict"] == "yes"


def test_audit_field_names_follow_the_taste_vocabulary():
    assert llm.AUDIT_FIELD_NAMES[4:] == taste.TASTE_FIELDS


def test_audit_cost():
    assert llm.audit_cost({"input_tokens": 1_000_000}) == pytest.approx(0.10)
    assert llm.audit_cost({"web_search_requests": 3}) == pytest.approx(0.03)


# ---------- 2. selection + verification ----------


def _result(verdict: str = "yes", **fields) -> dict:
    return {
        "verdict": verdict,
        "reason": f"{verdict} because",
        "brand_name": fields.pop("brand_name", None),
        "pre_launch": fields.pop("pre_launch", None),
        "fields": fields,
        "sources": [{"url": "https://src.example/a", "title": "A"}],
        "usage": {"input_tokens": 1000, "output_tokens": 100, "web_search_requests": 1},
        "cached": False,
    }


def _lookups(monkeypatch, by_name: dict) -> None:
    """Patch the audit lookup to answer per maker name (an Exception raises)."""

    def fake(session, name, country=None, context=None):
        answer = by_name.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(enrich, "lookup_maker_audit", fake)


def test_unaudited_scope_and_queue_order(test_engine):
    _add("Zzq Old", first_seen_at=_now() - timedelta(days=30))
    _add("Zzq New", first_seen_at=_now() - timedelta(days=1))
    ranked = _add("Zzq Ranked", first_seen_at=_now() - timedelta(days=90))
    _add("Zzq Reviewed", reviewed_at=_now())
    _add("Zzq Hidden", is_suppressed=True)
    _add("Zzq Done", audited_at=_now())
    _add(SEED_NAME)
    with session_scope() as session:
        session.add(
            Score(maker_id=ranked, preset=get_settings().default_preset,
                  value=1.0, rank=1, computed_at=_now())
        )
    with session_scope() as session:
        names = [m.canonical_name for m in iter_unaudited_makers(session)]
    assert names == ["Zzq Ranked", "Zzq New", "Zzq Old"]


def test_propose_keeps_checked_values_and_reports_the_rest(test_engine, monkeypatch):
    _add(DISC)
    monkeypatch.setattr(enrich, "http_get", lambda url: _Resolved())
    _lookups(monkeypatch, {DISC: _result(
        country="Japan",
        founded_year=2019,
        website="https://zzq.example",
        is_independent=False,
        aesthetic_tags=["modern", "skeletonized"],
        maker_type="solo-watchmaker",
    )})
    with session_scope() as session:
        [p] = propose_audits(session)
    assert p.fields == {
        "country": "Japan",
        "founded_year": 2019,
        "website": "https://zzq.example",
        "is_independent": False,
        "aesthetic_tags": ["modern"],
        "maker_type": "solo-watchmaker",
    }
    # A vocabulary gap is reported even though the rest of the list survived.
    assert "skeletonized" in p.rejected["aesthetic_tags"]


def test_propose_rejects_bad_values(test_engine, monkeypatch):
    _add(DISC)
    _lookups(monkeypatch, {DISC: _result(
        country="Atlantis",
        founded_year=1650,
        website="https://www.chrono24.com/zzq",
        is_independent="maybe",
        maker_type="wizard",
    )})
    with session_scope() as session:
        [p] = propose_audits(session)
    assert p.fields == {}
    assert set(p.rejected) == {
        "country", "founded_year", "website", "is_independent", "maker_type",
    }
    assert "chrono24" in p.rejected["website"]


def test_propose_no_verdict_drafts_no_fields(test_engine, monkeypatch):
    _add(DISC)
    _lookups(monkeypatch, {DISC: _result("no", country="Japan", brand_name="Zzq Real Brand")})
    with session_scope() as session:
        [p] = propose_audits(session)
    assert p.verdict == "no"
    assert p.fields == {}
    assert p.brand_name == "Zzq Real Brand"


def test_propose_drops_a_brand_that_is_the_makers_own_name(test_engine, monkeypatch):
    _add(DISC)
    _lookups(monkeypatch, {DISC: _result("yes", brand_name="zzq audit maker")})
    with session_scope() as session:
        [p] = propose_audits(session)
    assert p.brand_name is None


# ---------- 3. propose: failures, stopping, caching ----------


def test_failed_lookup_is_isolated_and_retried(test_engine, monkeypatch):
    a = _add("Zzq A", first_seen_at=_now())
    _add("Zzq B", first_seen_at=_now() - timedelta(days=1))
    _lookups(monkeypatch, {"Zzq A": RuntimeError("boom"), "Zzq B": _result()})
    with session_scope() as session:
        proposals = propose_audits(session)
        apply_audits(session, proposals)
    assert [p.error is not None for p in proposals] == [True, False]
    with session_scope() as session:
        assert [m.id for m in iter_unaudited_makers(session)] == [a]  # A retried next run


def test_systemic_failure_stops_the_run(test_engine, monkeypatch):
    for i in range(MAX_CONSECUTIVE_AUDIT_FAILURES + 1):
        _add(f"Zzq Fail {i}")

    def broken(*a, **k):
        raise RuntimeError("no API key")

    monkeypatch.setattr(enrich, "lookup_maker_audit", broken)
    with session_scope() as session:
        with pytest.raises(RuntimeError, match="in a row"):
            propose_audits(session)


def test_tests_never_reach_the_network_through_enrich():
    """conftest blocks enrich's own http_get, so its website check can't fetch
    a real site from a test — the test below once fetched fjordwatch.no."""
    ok, reason = enrich._verify_website("https://fjordwatch.no")
    assert not ok
    assert "Real HTTP blocked" in reason


def test_results_are_committed_as_they_arrive(session, test_engine, monkeypatch):
    """A paid result survives even if the run then dies before applying."""
    _add("Fjord Watch Co.")
    monkeypatch.setattr(enrich, "lookup_maker_audit", llm.lookup_maker_audit)
    _fake_api(monkeypatch, _reply(*_tool_answer_blocks(), stop_reason="tool_use"))
    propose_audits(session)
    session.rollback()  # the run "dies" here
    with session_scope() as fresh:
        assert fresh.query(LLMCache).count() == 1


# ---------- 4. apply ----------


def test_apply_fills_blanks_and_replaces_only_auto_values(test_engine, monkeypatch):
    mid = _add(
        DISC,
        country="France",                  # yours: no auto marker
        maker_type="microbrand",           # an earlier guess
        enriched_fields=["maker_type"],
    )
    monkeypatch.setattr(enrich, "http_get", lambda url: _Resolved())
    _lookups(monkeypatch, {DISC: _result(
        country="Japan", maker_type="solo-watchmaker", website="https://zzq.example",
    )})
    with session_scope() as session:
        written = apply_audits(session, propose_audits(session))
    m = _get(mid)
    assert m.country == "France"
    assert m.maker_type == "solo-watchmaker"
    assert m.website == "https://zzq.example"
    assert written == 2
    assert set(m.enriched_fields) == {"maker_type", "website"}


def test_apply_independence_rules(test_engine, monkeypatch):
    default = _add("Zzq Default")                         # True by default only
    yours = _add("Zzq Yours", is_independent=False)       # a deliberate False
    _lookups(monkeypatch, {
        "Zzq Default": _result(is_independent=False),
        "Zzq Yours": _result(is_independent=True),
    })
    with session_scope() as session:
        apply_audits(session, propose_audits(session))
    assert _get(default).is_independent is False
    assert "is_independent" in _get(default).enriched_fields
    assert _get(yours).is_independent is False


def test_apply_records_the_verdict_and_stamps(test_engine, monkeypatch):
    mid = _add(DISC)
    _lookups(monkeypatch, {DISC: _result("unsure", brand_name="Zzq Other")})
    with session_scope() as session:
        apply_audits(session, propose_audits(session))
    m = _get(mid)
    assert m.audit_verdict == "unsure"
    assert m.audit_reason == "unsure because"
    assert m.audit_brand == "Zzq Other"
    assert m.audit_sources == [{"url": "https://src.example/a", "title": "A"}]
    assert m.audited_at and m.enriched_at and m.taste_enriched_at
    with session_scope() as session:
        assert iter_unaudited_makers(session) == []


def test_apply_no_verdict_records_but_writes_no_fields(test_engine, monkeypatch):
    mid = _add(DISC)
    _lookups(monkeypatch, {DISC: _result("no", country="Japan", pre_launch=True)})
    with session_scope() as session:
        assert apply_audits(session, propose_audits(session)) == 0
    m = _get(mid)
    assert m.audit_verdict == "no"
    assert m.country is None
    assert m.is_pre_launch is None


def test_apply_flags_pre_launch_as_auto(test_engine, monkeypatch):
    mid = _add(DISC)
    _lookups(monkeypatch, {DISC: _result("yes", pre_launch=True)})
    with session_scope() as session:
        apply_audits(session, propose_audits(session))
    m = _get(mid)
    assert m.is_pre_launch is True
    assert "is_pre_launch" in m.enriched_fields


def _review_behind_the_sessions_back(session, maker_id: int) -> Maker:
    """A /review save landing mid-run, written straight to the DB. Returns the
    run's own copy of the maker — still showing it unreviewed — for the caller
    to hold, as a long run that keeps its makers loaded would. (Unheld rows
    drop out of the session's identity map and get re-read anyway, which
    would hide a missing re-read.)"""
    stale = session.get(Maker, maker_id)
    session.execute(
        text("UPDATE makers SET reviewed_at = CURRENT_TIMESTAMP WHERE id = :id"),
        {"id": maker_id},
    )
    assert stale.reviewed_at is None
    return stale


def test_apply_skips_a_maker_reviewed_mid_run(test_engine, monkeypatch):
    mid = _add(DISC)
    _lookups(monkeypatch, {DISC: _result(country="Japan")})
    with session_scope() as session:
        proposals = propose_audits(session)
        held = _review_behind_the_sessions_back(session, mid)  # noqa: F841
        assert apply_audits(session, proposals) == 0
    m = _get(mid)
    assert m.country is None
    assert m.audited_at is None


def test_factual_apply_skips_a_maker_reviewed_mid_run(test_engine, monkeypatch):
    mid = _add(DISC)
    monkeypatch.setattr(enrich, "lookup_maker_country", lambda *a, **k: "Japan")
    with session_scope() as session:
        proposals = propose_enrichments(session)
        held = _review_behind_the_sessions_back(session, mid)  # noqa: F841
        assert apply_enrichments(session, proposals) == 0
    assert _get(mid).country is None


def test_taste_apply_skips_a_maker_reviewed_mid_run(test_engine, monkeypatch):
    mid = _add(DISC)
    monkeypatch.setattr(enrich, "lookup_maker_taste", lambda *a, **k: {"maker_type": "microbrand"})
    with session_scope() as session:
        proposals = propose_taste_for_discovered(session)
        held = _review_behind_the_sessions_back(session, mid)  # noqa: F841
        assert apply_taste_proposals(session, proposals) == 0
    assert _get(mid).maker_type is None


# ---------- 5. CLI ----------


def test_cli_audit_dry_run_writes_nothing(test_engine, monkeypatch):
    mid = _add(DISC)
    _lookups(monkeypatch, {DISC: _result(country="Japan")})
    res = CliRunner().invoke(cli_app, ["enrich", "--audit"])
    assert res.exit_code == 0, res.stdout
    assert "Audit results" in res.stdout
    assert "Cost of this run" in res.stdout
    assert "Dry run" in res.stdout
    assert _get(mid).audited_at is None


def test_cli_audit_limit_and_commit(test_engine, monkeypatch):
    first = _add("Zzq First", first_seen_at=_now())
    second = _add("Zzq Second", first_seen_at=_now() - timedelta(days=1))
    _lookups(monkeypatch, {"Zzq First": _result(), "Zzq Second": _result()})
    res = CliRunner().invoke(cli_app, ["enrich", "--audit", "--limit", "1", "--commit"])
    assert res.exit_code == 0, res.stdout
    assert _get(first).audited_at is not None
    assert _get(second).audited_at is None


def test_cli_audit_cost_line_flags_failed_lookups(test_engine, monkeypatch):
    """A failed lookup returns no usage, but it still paid for its searches."""
    _add("Zzq First", first_seen_at=_now())
    _add("Zzq Second", first_seen_at=_now() - timedelta(days=1))
    _lookups(monkeypatch, {"Zzq First": None, "Zzq Second": _result()})
    res = CliRunner().invoke(cli_app, ["enrich", "--audit"])
    assert res.exit_code == 0, res.stdout
    assert "1 failed" in res.stdout
    assert "Not counted: the 1 failed lookup(s)" in res.stdout
    _lookups(monkeypatch, {"Zzq First": _result(), "Zzq Second": _result()})
    clean = CliRunner().invoke(cli_app, ["enrich", "--audit"])
    assert "Not counted" not in clean.stdout


def test_cli_audit_stops_on_a_systemic_failure(test_engine, monkeypatch):
    for i in range(MAX_CONSECUTIVE_AUDIT_FAILURES):
        _add(f"Zzq Fail {i}")

    def broken(*a, **k):
        raise RuntimeError("no API key")

    monkeypatch.setattr(enrich, "lookup_maker_audit", broken)
    res = CliRunner().invoke(cli_app, ["enrich", "--audit"])
    assert res.exit_code == 1
    assert "in a row" in res.stdout


# ---------- 6. the review card ----------


def test_card_shows_verdict_reason_and_sources(client, admin_on):
    _add(
        DISC,
        audit_verdict="no",
        audit_reason="A phrase from a headline, not a brand.",
        audit_sources=[{"url": "https://src.example/a", "title": "Source A"}],
    )
    r = client.get("/review")
    assert "Not a maker" in r.text
    assert "A phrase from a headline, not a brand." in r.text
    assert 'href="https://src.example/a"' in r.text
    assert ">src.example</a>" in r.text


def test_queue_puts_no_verdicts_first(test_engine):
    ranked = _add("Zzq Ranked Yes", audit_verdict="yes")
    _add("Zzq Junk", audit_verdict="no")
    with session_scope() as session:
        session.add(
            Score(maker_id=ranked, preset=get_settings().default_preset,
                  value=1.0, rank=1, computed_at=_now())
        )
    names = [d["canonical_name"] for d in _fetch_discovered_for_review()]
    assert names == ["Zzq Junk", "Zzq Ranked Yes"]


def test_brand_suggestion_links_the_merge_when_tracked(client, admin_on):
    brand = _add("Zzq Smith Watches")
    person = _add("Zzq Jeff Smith", audit_verdict="no", audit_brand="Zzq Smith Watches")
    r = client.get("/review")
    assert "already tracked as" in r.text
    assert f'href="/makers/{person}?confirm=merge&into={brand}"' in r.text


def test_brand_suggestion_hints_rename_when_untracked(client, admin_on):
    _add("Zzq Jeff Smith", audit_verdict="no", audit_brand="Zzq Brand Nobody Tracks")
    r = client.get("/review")
    assert "Zzq Brand Nobody Tracks" in r.text
    assert "not tracked yet" in r.text


def test_pre_launch_chip_checkbox_and_filter(client, admin_on):
    _add("Zzq Coming Soon", is_pre_launch=True, enriched_fields=["is_pre_launch"])
    _add("Zzq Shipped Earlier", is_pre_launch=True, reviewed_at=_now())
    _add("Zzq Shipping")
    page = client.get("/review").text
    assert "Pre-launch (2)" in page
    assert 'name="is_pre_launch" checked' in page
    flagged = client.get("/review?show=prelaunch").text
    assert "Zzq Coming Soon" in flagged and "Zzq Shipped Earlier" in flagged
    assert "Zzq Shipping" not in flagged


def test_save_takes_the_pre_launch_checkbox(client, admin_on):
    mid = _add(DISC, is_pre_launch=True, enriched_fields=["is_pre_launch"])
    client.post(
        f"/review/maker/{mid}",
        data={"canonical_name": DISC, "is_pre_launch": "on"},
        headers=HX,
    )
    m = _get(mid)
    assert m.is_pre_launch is True
    assert not m.enriched_fields  # now yours, not a draft
    other = _add("Zzq Other")
    client.post(f"/review/maker/{other}", data={"canonical_name": "Zzq Other"}, headers=HX)
    assert _get(other).is_pre_launch is False
