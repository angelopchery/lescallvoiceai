"""Re-ranking: a lexical arm, rank fusion, and redundancy suppression.

WHY NOT A CROSS-ENCODER
-----------------------
The textbook re-ranker is a cross-encoder scoring every (query, chunk) pair.
This runs inside a live phone call on a 2-core box that is already sharing
those cores with VAD, STT and TTS, and the turn budget is a caller waiting in
silence. A cross-encoder would add a model load, hundreds of milliseconds and a
second failure mode to a stage that currently costs 4ms. An LLM re-ranker adds
a network round trip to the same budget and, on the current key, competes with
the call's own quota.

So the re-ranker is built from what already works in this repo: the hybrid
retrieval that `shared/intelligence/search.py` uses for call search — a lexical
arm beside the vector arm, fused by Reciprocal Rank Fusion. It needs no model,
no dependency and no network, and it fixes the failure the measurements
actually showed: the vector arm putting the right document first but the wrong
chunk of it, and near-duplicate chunks crowding out the one that answers.

BM25 is implemented here rather than pulled in: it is thirty lines, the corpus
is one agent's documents held in memory, and a new dependency on the live-call
path has to earn itself.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

# \w with re.UNICODE keeps Devanagari and Gujarati tokens intact — this corpus
# and these callers are code-mixed, and an [a-z0-9] tokenizer silently drops
# every Hindi and Gujarati word in the query.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Deliberately small: English function words plus the romanized Hindi/Gujarati
# fillers that dominate this platform's transcripts. A large stoplist would
# start eating content words in a code-mixed sentence.
STOPWORDS = frozenset("""
a an the is are was were be been being am do does did doing done have has had having
what which who whom whose when where why how of for to in on at by with from as that
this these those it its your you our we us i they them their he she and or but if then
than so can could would should may might will shall about into over under again more
most some any no not only own same too very just now here there me my mine yours out up
down off also
hai ho hoon hu hain tha the thi kya kyu kyun aur ya par ke ki ka ko se me mein pe
aap tum main hum yeh ye woh wo koi kuch nahi nahin haan ha ji na to bhi hi
chhe che nathi ane athva tame hu ame teo aa te kem shu
ok okay hmm hm uh um yeah yes right sure achha accha theek
""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t]


def content_terms(text: str) -> list[str]:
    """Informative tokens — what a query is actually asking about."""
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 2]


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

@dataclass
class BM25:
    k1: float = 1.5
    b: float = 0.75

    def __post_init__(self):
        self.docs: list[Counter] = []
        self.lengths: list[int] = []
        self.df: Counter = Counter()
        self.avg_len: float = 0.0
        self.n: int = 0

    @classmethod
    def build(cls, texts: list[str], k1: float = 1.5, b: float = 0.75) -> "BM25":
        idx = cls(k1=k1, b=b)
        for text in texts:
            toks = tokenize(text)
            tf = Counter(toks)
            idx.docs.append(tf)
            idx.lengths.append(len(toks))
            for term in tf:
                idx.df[term] += 1
        idx.n = len(texts)
        idx.avg_len = (sum(idx.lengths) / idx.n) if idx.n else 0.0
        return idx

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def scores(self, query: str) -> np.ndarray:
        terms = [t for t in tokenize(query) if t not in STOPWORDS]
        out = np.zeros(self.n, dtype=np.float32)
        if not terms or not self.n:
            return out
        for term in terms:
            idf = self._idf(term)
            if idf <= 0:
                continue
            for i, tf in enumerate(self.docs):
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b *
                                       (self.lengths[i] / (self.avg_len or 1.0)))
                out[i] += idf * (f * (self.k1 + 1)) / denom
        return out


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def rrf_fuse(arms: list[list[int]], k: int = 60,
             weights: list[float] | None = None) -> dict[int, float]:
    """Reciprocal Rank Fusion over ranked lists of chunk indices.

    Same fusion (and same k) as the platform's call search, so the two
    retrieval systems cannot drift into disagreeing about what fusion means.
    """
    weights = weights or [1.0] * len(arms)
    scores: dict[int, float] = {}
    for arm, weight in zip(arms, weights):
        for rank, idx in enumerate(arm):
            scores[idx] = scores.get(idx, 0.0) + weight / (k + rank + 1)
    return scores


def normalize_scores(scores: dict[int, float]) -> dict[int, float]:
    """Min-max the relevance scores onto [0,1].

    MMR subtracts a redundancy term from a relevance term, so the two must be
    on the same scale. RRF scores are all ~1/(k+rank) — around 0.016 with k=60,
    and separated by ~0.0005 — while redundancy is a cosine in [0,1]. Fed raw,
    the redundancy term outweighs relevance by three orders of magnitude and
    MMR stops ranking by relevance at all: it selects the most *mutually
    dissimilar* chunks available. Measured on the golden set, that alone drove
    Recall@5 from 0.98 down to 0.70 while the candidate set still contained
    every answer (recall@candidates 1.00).
    """
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi <= lo:
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def mmr(order: list[int], sim_to_query: dict[int, float],
        similarity, lambda_: float = 0.75, limit: int = 5) -> list[int]:
    """Maximal Marginal Relevance — relevance minus redundancy.

    Documents here overlap by construction (chunking uses a 60-word overlap,
    and tenants upload the same profile to several agents), so the unfiltered
    top-3 is frequently the same passage twice. That wastes a context window
    which is re-sent on every turn of the call.

    `sim_to_query` must already be normalised to [0,1] — see normalize_scores.
    """
    scores = normalize_scores(sim_to_query)
    selected: list[int] = []
    pool = list(order)
    while pool and len(selected) < limit:
        best, best_score = None, -1e9
        for cand in pool:
            redundancy = max((similarity(cand, s) for s in selected), default=0.0)
            score = lambda_ * scores.get(cand, 0.0) - (1 - lambda_) * redundancy
            if score > best_score:
                best, best_score = cand, score
        selected.append(best)
        pool.remove(best)
    return selected


def jaccard(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
