"""Document upload, end to end below the HTTP layer (RUNBOOK §57).

What broke on 2026-09-24 and what these pin:

  * the OpenAI key ran dry and BOTH document stages depended on it — the
    field extractor (gpt-4o-mini) and the RAG embedding (text-embedding-3-small).
    A client's brief uploaded through the Copilot failed four times in a day
    with "429 insufficient_quota". Now: the extractor runs on the platform's
    own LLM endpoint (`resolve_llm`), and embeddings on Gemini's native API
    (`GeminiEmbeddingProvider`) with task types and a provider-reported token
    count.
  * a failed embed was invisible: "uploaded" either way. Now the upload
    response says `embedded`, the reason is kept next to the chunks, and
    `index_status` reports it.
  * the Builder read the whole body with no size cap and accepted any
    extension; the Copilot offered `.doc`, which python-docx cannot open.
    Now one gate (`_read_document_upload`) names what is wrong and the way out.
  * `Document.paragraphs` skipped every table — a client's objection/answer
    grid was extracted as nothing.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys

import httpx
import numpy as np
import pytest

from conftest import REPO_ROOT

from app.documents import config as rag_config
from app.documents import embeddings, extractor, store, yaml_filler


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# extractor: the type gate and the readers
# ---------------------------------------------------------------------------

def test_supported_types_and_the_way_out_for_the_rest():
    assert extractor.is_supported("Brochure.PDF")
    assert extractor.is_supported("prices.xlsx")
    assert extractor.unsupported_reason("script.docx") == ""
    # Each refusal says what to do instead, not just "no".
    assert ".docx" in extractor.unsupported_reason("old.doc")
    assert "PDF" in extractor.unsupported_reason("deck.pptx")
    assert "extension" in extractor.unsupported_reason("README").lower()
    assert "'.exe'" in extractor.unsupported_reason("setup.exe")
    # The accept list the pickers use is derived from the same table.
    assert extractor.ACCEPT.startswith(".pdf,") and ".xlsx" in extractor.ACCEPT
    assert ".doc," not in extractor.ACCEPT + ","


def test_extract_text_refuses_doc_before_touching_bytes():
    with pytest.raises(ValueError) as exc:
        extractor.extract_text(b"\xd0\xcf\x11\xe0", "old.doc")
    assert "save as .docx" in str(exc.value)


def test_plain_text_formats_bom_and_renamed_binaries():
    assert extractor.extract_text("﻿hello".encode("utf-8"), "a.txt") == "hello"
    assert extractor.extract_text(b"a,b\n1,2", "t.csv") == "a,b\n1,2"
    assert extractor.extract_text("# T\nbody".encode(), "n.md").startswith("# T")
    assert extractor.extract_text("hi".encode("utf-16"), "u.txt") == "hi"
    with pytest.raises(ValueError) as exc:
        extractor.extract_text(b"PK\x03\x04" + b"\x00" * 64, "renamed.txt")
    assert "real extension" in str(exc.value)


def test_docx_tables_are_read_in_document_order():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("Intro")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Objection", "Answer"
    t.cell(1, 0).text, t.cell(1, 1).text = "Too expensive", "Quote the EMI"
    d.add_paragraph("Outro")
    buf = io.BytesIO()
    d.save(buf)
    text = extractor.extract_text(buf.getvalue(), "script.docx")
    assert (text.index("Intro") < text.index("Objection | Answer")
            < text.index("Too expensive | Quote the EMI") < text.index("Outro"))


def test_xlsx_every_sheet_row_by_row_values_not_formulas():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(["Unit", "Price"])
    ws.append(["2BHK", 4500000])
    ws.append([None, None])
    wb.create_sheet("Empty")
    buf = io.BytesIO()
    wb.save(buf)
    text = extractor.extract_text(buf.getvalue(), "p.xlsx")
    assert text.startswith("## Prices")
    assert "Unit | Price" in text and "2BHK | 4500000" in text
    assert "Empty" not in text


# ---------------------------------------------------------------------------
# Gemini embedding provider
# ---------------------------------------------------------------------------

def _gemini(handler, **kw):
    p = embeddings.GeminiEmbeddingProvider(model="gemini-embedding-2", dim=4,
                                           api_key="k", **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    return p


def test_gemini_batches_task_types_dimensions_and_tokens():
    seen: list[tuple[dict, dict]] = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append((body, dict(req.headers)))
        n = len(body["requests"])
        return httpx.Response(200, json={
            "embeddings": [{"values": [1.0, 0.0, 0.0, 0.0]}] * n,
            "usageMetadata": {"promptTokenCount": 3 * n}})

    p = _gemini(handler, batch_size=2)
    vecs = p.embed(["a", "b", "c"])
    assert vecs.shape == (3, 4) and vecs.dtype == np.float32
    assert p.last_tokens == 9                     # provider-reported, summed
    assert len(seen) == 2                         # 2 + 1, the batch cap
    body, headers = seen[0]
    assert headers["x-goog-api-key"] == "k"
    assert seen[0][1]["host"] == "generativelanguage.googleapis.com"
    req = body["requests"][0]
    assert req["model"] == "models/gemini-embedding-2"
    assert req["taskType"] == "RETRIEVAL_DOCUMENT"
    assert req["outputDimensionality"] == 4
    assert req["content"]["parts"][0]["text"] == "a"

    p.embed_queries(["where is it"])
    assert seen[-1][0]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"

    p.embed([""])                                  # never an empty part (400)
    assert seen[-1][0]["requests"][0]["content"]["parts"][0]["text"] == " "
    assert p.embed([]).shape == (0, 4)


def test_gemini_error_carries_the_providers_sentence_and_drops_the_client():
    def handler(req):
        return httpx.Response(429, json={"error": {"message": "Quota exceeded for metric X"}})
    p = _gemini(handler)
    with pytest.raises(RuntimeError) as exc:
        p.embed(["a"])
    assert "429" in str(exc.value) and "Quota exceeded" in str(exc.value)
    assert p._client is None                     # a hung connection is not reused


def test_gemini_vector_count_mismatch_is_an_error_not_garbage():
    def handler(req):
        return httpx.Response(200, json={"embeddings": [{"values": [1, 0, 0, 0]}]})
    p = _gemini(handler)
    with pytest.raises(RuntimeError) as exc:
        p.embed(["a", "b"])
    assert "1 vectors for 2 texts" in str(exc.value)


def test_gemini_reads_the_ocr_keys_when_the_configured_one_is_unset(monkeypatch):
    monkeypatch.setattr(rag_config, "EMBED_API_KEY_ENV", "RAG_TEST_KEY_UNSET")
    monkeypatch.delenv("RAG_TEST_KEY_UNSET", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert embeddings.GeminiEmbeddingProvider(model="m", dim=4).available() is False
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert embeddings.GeminiEmbeddingProvider(model="m", dim=4).available() is True


def test_build_provider_follows_the_model_then_the_config(monkeypatch):
    assert isinstance(embeddings.build_provider(model="text-embedding-3-small"),
                      embeddings.OpenAIEmbeddingProvider)
    assert isinstance(embeddings.build_provider(model="gemini-embedding-2"),
                      embeddings.GeminiEmbeddingProvider)
    monkeypatch.setattr(rag_config, "EMBED_PROVIDER", "openai")
    assert isinstance(embeddings.build_provider(), embeddings.OpenAIEmbeddingProvider)
    monkeypatch.setattr(rag_config, "EMBED_PROVIDER", "gemini")
    p = embeddings.build_provider()
    assert isinstance(p, embeddings.GeminiEmbeddingProvider)
    assert p.model == rag_config.EMBED_MODEL and p.dim == rag_config.EMBED_DIM


def test_platform_default_is_gemini_embedding_2():
    if os.environ.get("RAG_EMBED_PROVIDER") or os.environ.get("RAG_EMBED_MODEL"):
        pytest.skip("environment overrides the default")
    assert rag_config.EMBED_PROVIDER == "gemini"
    assert rag_config.EMBED_MODEL == "gemini-embedding-2"
    assert rag_config.EMBED_DIM == 768
    assert rag_config.EMBED_API_KEY_ENV == "LLM_API_KEY"
    from shared import ai_pricing
    assert ai_pricing.embedding_rate("gemini-embedding-2") == 0.20


# ---------------------------------------------------------------------------
# embed status: a failure leaves a reason, a success clears it
# ---------------------------------------------------------------------------

class _Provider:
    provider, model, dim = "fake", "fake-embed-v1", 4

    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.last_tokens = 0
        self.query_calls = 0

    def available(self):
        return True

    def embed(self, texts):
        if self.fail:
            raise RuntimeError(self.fail)
        self.last_tokens = len(texts)
        return np.ones((len(texts), self.dim), dtype=np.float32)

    def embed_queries(self, texts):
        self.query_calls += 1
        return self.embed(texts)


@pytest.fixture()
def agent_dir(tmp_path, monkeypatch):
    from app.documents import cache as cache_mod
    from app.documents import retrieval
    agents = tmp_path / "agents"
    (agents / "acme").mkdir(parents=True)
    monkeypatch.setattr(store, "_AGENTS_DIR", agents)
    monkeypatch.setattr(embeddings, "_AGENTS_DIR", agents)
    cache_mod.reset_cache()
    retrieval._index_cache.clear()
    yield agents
    embeddings.set_provider(None)
    cache_mod.reset_cache()
    retrieval._index_cache.clear()


def test_failed_embed_leaves_a_readable_reason_and_success_clears_it(agent_dir):
    meta = store.save_document("acme", "a.txt", b"hello world", "hello world " * 30,
                               "", "General", "")
    failing = _Provider(fail="gemini embeddings HTTP 429: over quota")
    assert embeddings.embed_document("acme", meta["doc_id"], failing) is False
    status = embeddings.index_status("acme", failing)
    row = status["documents"][0]
    assert row["state"] == "not_embedded"
    assert "429" in row["detail"] and row["failed_at"]
    assert meta["doc_id"] in status["needs_reindex"]
    assert status["runtime"]["available"] is True

    ok = _Provider()
    assert embeddings.embed_document("acme", meta["doc_id"], ok) is True
    assert embeddings.embed_error("acme", meta["doc_id"]) is None
    row = embeddings.index_status("acme", ok)["documents"][0]
    assert row["state"] == "ok" and row["model"] == "fake-embed-v1"


def test_embed_query_uses_the_query_task_when_the_provider_has_one():
    p = _Provider()
    vec, tokens = embeddings.embed_query("where is swarit", p)
    assert p.query_calls == 1 and tokens == 1 and vec.shape == (4,)
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-6


def test_find_document_by_hash(agent_dir):
    meta = store.save_document("acme", "b.txt", b"same bytes", "same bytes", "", "General", "")
    assert len(meta["sha256"]) == 64 and meta["size_bytes"] == 10
    assert store.find_document_by_hash("acme", meta["sha256"])["doc_id"] == meta["doc_id"]
    assert store.find_document_by_hash("acme", "0" * 64) is None
    assert store.find_document_by_hash("acme", "") is None


# ---------------------------------------------------------------------------
# Builder endpoints — the gate, the honest reply, the duplicate
# ---------------------------------------------------------------------------

class _Builder:
    """The dashboard module plus the document modules AS THE DASHBOARD
    IMPORTS THEM. The dashboard says `backend.app.documents.store`; these
    tests say `app.documents.store` — two module objects, two `_AGENTS_DIR`s,
    two provider singletons. Patching only the test-side one sent the first
    version of this test's upload into the REAL agents directory and its
    embed to the real Gemini endpoint."""

    def __init__(self, builder, store_mod, emb_mod):
        self.api, self.store, self.emb = builder, store_mod, emb_mod


@pytest.fixture()
def builder(tmp_path, monkeypatch, agent_dir):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import backend.app.documents.embeddings as b_emb
    import backend.app.documents.store as b_store
    from backend.app.documents import cache as b_cache
    from backend.app.documents import retrieval as b_retrieval
    monkeypatch.setattr(builder, "_AGENTS_DIR", agent_dir)
    monkeypatch.setattr(b_store, "_AGENTS_DIR", agent_dir)
    monkeypatch.setattr(b_emb, "_AGENTS_DIR", agent_dir)
    monkeypatch.setattr(builder, "_billing_llm_gate", lambda owner, surface: None)
    monkeypatch.setattr(builder, "_ctx_owner", lambda: "tenant")
    b_cache.reset_cache()
    b_retrieval._index_cache.clear()
    yield _Builder(builder, b_store, b_emb)
    b_emb.set_provider(None)
    b_cache.reset_cache()
    b_retrieval._index_cache.clear()


def _upload(data: bytes, filename: str):
    from starlette.datastructures import UploadFile
    return UploadFile(io.BytesIO(data), filename=filename, size=len(data))


def test_upload_gate_names_type_size_and_empty(builder, monkeypatch):
    from fastapi import HTTPException
    api = builder.api
    with pytest.raises(HTTPException) as exc:
        _run(api.upload_document("acme", _upload(b"x", "old.doc"), "", "General", ""))
    assert exc.value.status_code == 415 and ".docx" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        _run(api.upload_document("acme", _upload(b"", "a.txt"), "", "General", ""))
    assert exc.value.status_code == 400
    monkeypatch.setattr(api, "_DOC_MAX_BYTES", 8)
    with pytest.raises(HTTPException) as exc:
        _run(api.upload_document("acme", _upload(b"0123456789", "a.txt"), "", "General", ""))
    assert exc.value.status_code == 413 and "limit is 0 MB" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        _run(api.extract_from_document(_upload(b"x", "deck.pptx")))
    assert exc.value.status_code == 415
    assert builder.store.list_documents("acme") == []   # nothing written by a refusal


def test_upload_reports_the_embed_outcome_and_answers_a_duplicate_with_the_original(builder):
    api = builder.api
    body = ("Pramukh Swarit is a residential project in Vapi. " * 20).encode()
    builder.emb.set_provider(_Provider(fail="gemini embeddings HTTP 429: over quota"))
    res = _run(api.upload_document("acme", _upload(body, "swarit.txt"), "Pramukh", "Brochure", ""))
    assert res["status"] == "uploaded" and res["embedded"] is False
    assert "429" in res["index_state"]["detail"]
    assert res["doc_id"] in res["index"]["needs_reindex"]
    assert res["meta"]["org_name"] == "Pramukh" and res["meta"]["sha256"]

    builder.emb.set_provider(_Provider())
    dup = _run(api.upload_document("acme", _upload(body, "swarit-again.txt"), "", "General", ""))
    assert dup["status"] == "duplicate" and dup["doc_id"] == res["doc_id"]
    assert dup["embedded"] is False                    # still the original's state
    assert len(builder.store.list_documents("acme")) == 1

    ok = _run(api.upload_document("acme", _upload(body + b" v2", "swarit-v2.txt"), "", "General", ""))
    assert ok["status"] == "uploaded" and ok["embedded"] is True
    assert ok["index_state"]["state"] == "ok"
    assert len(builder.store.list_documents("acme")) == 2

    # The repair path: re-index embeds the one that failed.
    monkeypatch_free = _run(api.reindex_agent_documents("acme"))
    assert res["doc_id"] in monkeypatch_free["reindexed"]
    assert monkeypatch_free["index"]["needs_reindex"] == []


def test_extract_yaml_error_is_a_502_with_the_extractors_sentence(builder, monkeypatch):
    from fastapi import HTTPException
    import backend.app.documents.yaml_filler as b_filler
    api = builder.api
    meta = builder.store.save_document("acme", "c.txt", b"text", "some text", "", "General", "")
    monkeypatch.setattr(b_filler, "extract_fields_from_text",
                        lambda text: {"status": "error", "code": "llm_quota",
                                      "error": "The extraction model (m) is out of quota"})
    with pytest.raises(HTTPException) as exc:
        _run(api.extract_yaml_from_document("acme", meta["doc_id"]))
    assert exc.value.status_code == 502 and "out of quota" in exc.value.detail

    monkeypatch.setattr(b_filler, "extract_fields_from_text",
                        lambda text: {"status": "ok", "fields": {"company_name": "Acme"},
                                      "missing": [], "model": "m", "chunks": 1,
                                      "truncated": False})
    res = _run(api.extract_from_document(_upload(b"Acme sells fabric", "brief.txt")))
    assert res["fields"] == {"company_name": "Acme"}
    assert res["filename"] == "brief.txt" and res["char_count"] == 17


# ---------------------------------------------------------------------------
# yaml_filler: which model, and how it is called
# ---------------------------------------------------------------------------

def _clear_llm_env(monkeypatch):
    for name in ("DOC_EXTRACT_BASE_URL", "DOC_EXTRACT_API_KEY", "DOC_EXTRACT_MODEL",
                 "DOC_EXTRACT_REASONING_EFFORT", "LLM_BASE_URL", "LLM_API_KEY",
                 "LLM_MODEL", "COPILOT_LLM_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_resolve_llm_prefers_the_platform_endpoint_and_the_copilot_model(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setenv("LLM_API_KEY", "gem")
    monkeypatch.setenv("LLM_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("COPILOT_LLM_MODEL", "gemini-3.6-flash")
    monkeypatch.setenv("OPENAI_API_KEY", "oai")
    r = yaml_filler.resolve_llm()
    assert r["provider"] == "gemini" and r["api_key"] == "gem"
    assert r["model"] == "gemini-3.6-flash"          # the builder's bucket, not the live-call one
    assert r["reasoning_effort"] == "low"

    monkeypatch.setenv("DOC_EXTRACT_MODEL", "gemini-3.5-flash")
    assert yaml_filler.resolve_llm()["model"] == "gemini-3.5-flash"

    monkeypatch.delenv("COPILOT_LLM_MODEL")
    monkeypatch.delenv("DOC_EXTRACT_MODEL")
    assert yaml_filler.resolve_llm()["model"] == "gemini-3.5-flash-lite"

    # No platform key → the legacy path, exactly as before.
    monkeypatch.delenv("LLM_API_KEY")
    r = yaml_filler.resolve_llm()
    assert r["provider"] == "openai" and r["api_key"] == "oai"
    assert r["model"] == "gpt-4o-mini" and r["base_url"] == "" and r["reasoning_effort"] == ""


def test_extract_fields_unconfigured_is_a_named_error(monkeypatch):
    _clear_llm_env(monkeypatch)
    r = yaml_filler.extract_fields_from_text("anything")
    assert r["status"] == "error" and r["code"] == "llm_unconfigured"


class _Msg:
    def __init__(self, content):
        self.content = content


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _Msg(content)})()]


def _fake_openai(monkeypatch, answer):
    """Install a fake `openai.OpenAI` whose chat.completions.create calls
    `answer(kwargs)` and records every call."""
    import openai
    calls: list[dict] = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return answer(kwargs)

    class _Chat:
        completions = _Completions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append({"__init__": kwargs})
            self.chat = _Chat()

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    return calls


def test_extract_fields_json_mode_chunking_and_merge(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setenv("LLM_API_KEY", "gem")
    monkeypatch.setenv("COPILOT_LLM_MODEL", "gemini-3.6-flash")
    monkeypatch.setattr(yaml_filler, "_CHUNK_CHARS", 20)
    monkeypatch.setattr(yaml_filler, "_MAX_CHUNKS", 2)

    def answer(kwargs):
        # One field name unique to each themed pass (the policy pass also
        # lists `opening_greeting_by_language`, so the opener is not unique).
        prompt = kwargs["messages"][0]["content"]
        if "portfolio_prompt_text" in prompt:
            return _Resp('{"company_name": "Acme", "locations": ["Surat"]}')
        if "objection_playbook" in prompt:
            return _Resp('```json\n{"opening_greeting": "Hi there"}\n```')
        return _Resp("Sure! {\"languages\": {\"supported\": [\"en\"]}} hope that helps")

    calls = _fake_openai(monkeypatch, answer)
    text = "x" * 50                                  # 3 chunks of 20 → capped at 2
    r = yaml_filler.extract_fields_from_text(text)
    assert r["status"] == "ok", r
    assert r["model"] == "gemini-3.6-flash" and r["chunks"] == 2 and r["truncated"] is True
    assert r["fields"]["company_name"] == "Acme"
    assert r["fields"]["locations"] == ["Surat"]     # deduped across chunks
    assert r["fields"]["opening_greeting"] == "Hi there"
    assert r["fields"]["languages"] == {"supported": ["en"]}
    init = calls[0]["__init__"]
    assert init["api_key"] == "gem" and "googleapis" in init["base_url"]
    made = [c for c in calls if "model" in c]
    assert len(made) == 6                             # 3 passes × 2 chunks
    assert all(c["response_format"] == {"type": "json_object"} for c in made)
    assert all(c["reasoning_effort"] == "low" for c in made)
    assert all(c["max_tokens"] >= 8000 for c in made)


def test_extract_fields_quota_error_is_a_sentence_for_the_user(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setenv("LLM_API_KEY", "gem")

    class Quota(Exception):
        status_code = 429

    def answer(kwargs):
        raise Quota("Error code: 429 - insufficient_quota")

    _fake_openai(monkeypatch, answer)
    r = yaml_filler.extract_fields_from_text("some document")
    assert r["status"] == "error" and r["code"] == "llm_quota"
    assert "out of quota" in r["error"] and "gemini" in r["error"]


def test_extract_fields_drops_rejected_parameters_once(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("DOC_EXTRACT_MODEL", "some-compat-model")

    class Bad(Exception):
        status_code = 400

    def answer(kwargs):
        if "response_format" in kwargs or "reasoning_effort" in kwargs:
            raise Bad("unknown parameter: response_format")
        return _Resp("{}")

    calls = _fake_openai(monkeypatch, answer)
    r = yaml_filler.extract_fields_from_text("doc")
    assert r["status"] == "ok" and r["fields"] == {}
    made = [c for c in calls if "model" in c]
    assert len(made) == 6 and sum("response_format" in c for c in made) == 3
