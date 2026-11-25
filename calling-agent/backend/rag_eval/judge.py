"""Answer generation and LLM-judged answer quality.

Two separable things, because they fail differently:

  Generator — answers each golden question from the retrieved evidence, using
              the SAME grounding contract the live agent uses
              (`documents.grounding`). This is what turns a retrieval score
              into an end-to-end one, and it is the only way to measure the
              half of abstention that retrieval cannot decide.

  Judge     — a second model scoring faithfulness (is every claim in the answer
              supported by the supplied evidence?) and answer relevance. It
              never sees the expected answer for faithfulness: an answer that
              is correct because the model knew it from pre-training, and not
              from the retrieved context, is a RAG failure, and grading against
              a reference answer would score it as a success.

Both are opt-in. The evaluation's default mode is retrieval-only precisely so
the everyday run stays free, offline and deterministic; generation costs real
tokens against a key whose free tier this platform has already measured at ~20
requests per day (see `voice/prompt_cache.py`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from app.documents import grounding

logger = logging.getLogger(__name__)

from .judge_markers import ABSTAIN_MARKERS, looks_like_abstention  # noqa: F401


class _Client:
    """OpenAI-compatible client for whichever endpoint is configured.

    `provider="llm"` is the endpoint the live call path uses, so a faithfulness
    number describes the model that actually talks to callers. `provider=
    "openai"` exists because that key's free tier throttles hard, and a
    measurement you cannot finish is not a measurement.
    """

    def __init__(self, provider: str = "llm", model: str | None = None):
        self.provider = provider
        if provider == "openai":
            self.base_url = None
            self.api_key = os.environ.get("OPENAI_API_KEY", "")
            self.model = model or "gpt-4o-mini"
        else:
            self.base_url = os.environ.get("LLM_BASE_URL") or None
            self.api_key = os.environ.get("LLM_API_KEY", "")
            self.model = model or os.environ.get("LLM_MODEL", "gemini-3.5-flash")
        self._client = None

    def get(self):
        if self._client is None:
            import openai
            kwargs = {"api_key": self.api_key, "timeout": 60, "max_retries": 1}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def complete(self, system: str, user: str, max_tokens: int = 400) -> dict:
        t0 = time.perf_counter()
        resp = self.get().chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0,
        )
        usage = getattr(resp, "usage", None)
        return {
            "text": (resp.choices[0].message.content or "").strip(),
            "model": self.model,
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "llm_ms": round((time.perf_counter() - t0) * 1000, 2),
        }


_JUDGE_SYSTEM = (
    "You grade a RAG system. You are given a QUESTION, the EVIDENCE that was "
    "supplied to the answering model, and its ANSWER.\n"
    "Grade two things, independently:\n"
    "1. faithfulness (0.0-1.0): is every factual claim in the ANSWER supported "
    "by the EVIDENCE? A claim that is true in the real world but absent from "
    "the EVIDENCE is UNFAITHFUL — score it low. An answer that declines to "
    "answer, or offers to check with the team, is fully faithful (1.0) because "
    "it asserts nothing unsupported.\n"
    "2. answer_relevance (0.0-1.0): does the ANSWER address the QUESTION that "
    "was asked? A correct-but-unrelated answer scores low. A refusal scores "
    "1.0 only when the EVIDENCE genuinely does not contain the answer.\n"
    "Also list any unsupported claims.\n"
    'Reply with ONLY a JSON object: {"faithfulness": 0.0, "answer_relevance": '
    '0.0, "unsupported": ["..."], "note": "one short sentence"}'
)


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


class Generator:
    def __init__(self, judge: bool = False, model: str | None = None,
                 judge_model: str | None = None, provider: str = "llm",
                 judge_provider: str | None = None,
                 system_prompt: str | None = None):
        self.client = _Client(provider, model)
        # Overridden when measuring the pre-upgrade pipeline, which had no
        # grounding contract — see legacy_baseline.LEGACY_SYSTEM_PROMPT.
        self.system_prompt = system_prompt or grounding.eval_system_prompt()
        self.judge_enabled = judge
        self.judge_client = (_Client(judge_provider or provider, judge_model)
                             if judge else None)
        self.errors = 0

    def answer(self, case, outcome) -> dict:
        """Generate an answer for one case from its retrieved evidence."""
        evidence = outcome.context or grounding.NO_EVIDENCE_BLOCK
        user = f"{evidence}\n\nQUESTION: {case.question}"
        out: dict = {}
        try:
            gen = self.client.complete(self.system_prompt, user)
        except Exception as exc:
            self.errors += 1
            logger.warning("generation failed for %s: %r", case.id, exc)
            return {"answer": "", "error": f"{exc.__class__.__name__}", "refused": False}

        answer = gen["text"]
        out.update({
            "answer": answer,
            "refused": looks_like_abstention(answer),
            "model": gen["model"],
            "input_tokens": gen["input_tokens"],
            "output_tokens": gen["output_tokens"],
            "llm_ms": gen["llm_ms"],
        })
        try:
            from shared.ai_pricing import llm_cost
            out["cost_inr"] = llm_cost(gen["model"], gen["input_tokens"],
                                       gen["output_tokens"])
        except Exception:
            out["cost_inr"] = None

        if self.judge_enabled and answer:
            out.update(self._judge(case, outcome, answer))
        return out

    def _judge(self, case, outcome, answer: str) -> dict:
        evidence = outcome.context or "(no evidence was retrieved)"
        user = (f"QUESTION:\n{case.question}\n\nEVIDENCE:\n{evidence}\n\n"
                f"ANSWER:\n{answer}")
        try:
            res = self.judge_client.complete(_JUDGE_SYSTEM, user, max_tokens=300)
        except Exception as exc:
            logger.warning("judge failed for %s: %r", case.id, exc)
            return {"judge_note": f"judge unavailable: {exc.__class__.__name__}"}
        parsed = _parse_json(res["text"])
        if not parsed:
            return {"judge_note": "judge returned unparseable output"}
        out = {}
        for key in ("faithfulness", "answer_relevance"):
            try:
                out[key] = max(0.0, min(1.0, float(parsed.get(key))))
            except (TypeError, ValueError):
                pass
        if parsed.get("note"):
            out["judge_note"] = str(parsed["note"])[:200]
        if parsed.get("unsupported"):
            out["judge_unsupported"] = [str(x)[:120] for x in parsed["unsupported"]][:5]
        return out
