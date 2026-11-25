# Golden dataset — how to add and change questions

`docs_rag.jsonl` is the evaluation set for the document RAG that grounds live
calls. One JSON object per line.

## What is in here, and what is not

Everything in `../corpus/` is **real**: it is a verbatim copy of content this
platform serves today — the one document a tenant actually uploaded through the
Agent Builder, plus the knowledge paragraphs nine live agents read from on every
call. `../corpus/manifest.json` records each file's source path, a sha256 and
any redaction. `python -m rag_eval.build_corpus --check` fails if a copy has
drifted from its source.

The **questions** are authored against that corpus (`metadata.provenance:
"authored from corpus"`). They are not transcribed caller turns. When real
caller questions become available, add them with
`"provenance": "production transcript"` so the two are never conflated in a
report.

## Fields

| field | required | meaning |
|---|---|---|
| `id` | yes | stable, unique. Never renumber — historical runs reference it |
| `question` | yes | exactly what a caller would say, including code-mixing |
| `expected_answer` | yes | prose, for humans reading the report |
| `expected_sources` | yes if `answerable` | `[{doc, anchor, relevance}]` |
| `category` | yes | see below |
| `difficulty` | yes | `easy` / `medium` / `hard` |
| `answerable` | yes | `false` means abstaining is the CORRECT behaviour |
| `required_facts` | no | substrings a good answer contains |
| `forbidden_claims` | no | substrings that can only come from outside the corpus |
| `metadata` | no | free-form; `lang`, `provenance`, `note` are conventional |

### `expected_sources` — anchors, not chunk ids

```json
{"doc": "dhronai_profile", "anchor": "Six Stage Implementation Approach", "relevance": 2}
```

* `doc` — a filename stem in `../corpus/`.
* `anchor` — a **verbatim phrase** from that document. A retrieved chunk counts
  as relevant when it comes from `doc` and contains `anchor` (whitespace
  collapsed, case-insensitive).
* `relevance` — `2` primary evidence, `1` supporting. Only NDCG uses the grade.

Anchors rather than chunk indices because chunk size is a tuned parameter
(`RAG_CHUNK_WORDS`). A dataset keyed on chunk ids silently stops meaning
anything the first time someone retunes chunking — the metrics keep computing.

`validate()` fails the run if an anchor no longer occurs in its document, which
is the tripwire for corpus drift.

## Categories

| category | what it tests |
|---|---|
| `direct_factual` | one fact, one place |
| `multi_hop` | needs two chunks or two documents combined |
| `contextual` | needs the surrounding argument, not a lookup |
| `ambiguous` | under-specified; asking a clarifying question is correct |
| `no_answer` | the corpus genuinely does not contain it — must abstain |
| `wrong_context` | a similar-but-incorrect document exists and must not win |
| `long_context` | the real ask is buried in preamble |
| `edge_case` | chunk boundaries, filler turns, code-mixed Hindi/Gujarati |

## Adding a question

1. **Verify the answer is really in (or really absent from) the corpus.**
   `grep -ri "warranty" ../corpus/` — do this before writing the case, not
   after. Two of the original no-answer questions were wrong: the corpus *did*
   answer the EMI one, and "ISO" only matched inside the word "isolated".
2. Copy the phrase you expect retrieval to find into `anchor`, verbatim.
3. For a `no_answer` case, put only **assertion-shaped** phrases in
   `forbidden_claims`. Words the question itself supplies ("employees",
   "per camera") appear in a perfectly good refusal and would be scored as
   hallucinations.
4. Re-run and re-baseline:

```bash
cd calling-agent/backend
../venv/bin/python -m rag_eval.runner --live          # --live embeds the new question once
../venv/bin/python -m rag_eval.runner --save-baseline current
git add rag_eval/vectors/frozen.npz rag_eval/baselines/current.json
```

The first `--live` run extends the frozen vector cache so every later run — and
CI — stays offline and deterministic. Commit the `.npz` with the question.

## Running

```bash
../venv/bin/python -m rag_eval.runner                      # offline, free, deterministic
../venv/bin/python -m rag_eval.runner --fresh              # real API — the only real latency
../venv/bin/python -m rag_eval.runner --generate --judge --gen-provider openai
../venv/bin/python -m rag_eval.runner --compare baselines/legacy_shipped.json
../venv/bin/python -m rag_eval.sweep --live                # parameter grid
```
