"""Knowledge binding (v72) - dataset-backed answers over the phone.

A voice agent can bind a DATASET as its knowledge: rows carry a question
(text) column and an answer column. At every voice turn the caller's
transcript is scored against the bound dataset and the best matches ride
the handler envelope's ``metadata.knowledge`` - so the phone agent
answers from YOUR data, not from a scaffold's imagination.

The retrieval is deliberately simple and honest:

* lexical scoring (tokenized, stopword-filtered, idf-weighted overlap
  with length normalization) - deterministic, offline, explainable;
* the matches carry their score and row evidence, so a handler (or a
  human reading the transcript) can SEE what the answer was grounded on;
* an unknown dataset or column fails loud; a query that matches nothing
  returns an empty list - never a made-up answer.

LLM/retrieval upgrades can replace this later; the CONTRACT (matches in,
envelope metadata out) stays.
"""

from __future__ import annotations

import math
import re

from sqlalchemy.ext.asyncio import AsyncSession

from . import datasets as ds_svc

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A small English stopword set - enough to keep "what are your hours"
# from drowning in "are"/"your" noise without pretending to be an NLP kit.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "of", "at", "by", "for",
    "with", "about", "to", "from", "in", "on", "is", "are", "was", "were",
    "be", "been", "am", "do", "does", "did", "can", "could", "will",
    "would", "shall", "should", "i", "you", "we", "they", "it", "this",
    "that", "what", "when", "where", "who", "how", "my", "your", "our",
    "me", "us", "him", "her", "them", "as", "so", "not", "no", "yes",
})


class KnowledgeError(ValueError):
    """Honest 4xx-grade knowledge failures (unknown dataset/column)."""


def tokenize(text: str) -> list[str]:
    toks = _TOKEN_RE.findall(str(text or "").lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) > 1]


def score_match(query_tokens: list[str], doc_tokens: list[str]) -> float:
    """IDF-weighted overlap with length normalization (BM25-flavored).

    Deterministic: identical query/doc pairs always score identically.
    """
    if not query_tokens or not doc_tokens:
        return 0.0
    tf: dict[str, int] = {}
    for t in doc_tokens:
        tf[t] = tf.get(t, 0) + 1
    score = 0.0
    for term in query_tokens:
        f = tf.get(term, 0)
        if f:
            # saturating term frequency (k1=1.2) * idf-ish weight
            score += (f / (f + 1.2)) * (1.0 + math.log(1.0 + 1.0 / f))
    # length normalization (b=0.75, avg doc length 1.0 as the corpus norm)
    return score / (0.25 + 0.75 * math.sqrt(max(1, len(doc_tokens))))


async def load_knowledge_dataset(db: AsyncSession, dataset_id: str, owner_id: str | None):
    """Owner-scoped dataset load for a knowledge binding (404-grade)."""
    ds = await ds_svc.get_dataset(db, dataset_id, owner_id)
    if ds is None:
        raise KnowledgeError(f"knowledge dataset {dataset_id!r} not found")
    return ds


def _column_names(ds) -> list[str]:
    return [str((c or {}).get("name") or "") for c in (ds.schema_json or [])
            if str((c or {}).get("name") or "")]


async def knowledge_search(db: AsyncSession, *, dataset_id: str, query: str,
                           text_column: str, answer_column: str | None = None,
                           top_k: int = 1, owner_id: str | None = None) -> dict:
    """Score the bound dataset against the query; return the top matches.

    Each match: {score, question, answer, row}. The answer column
    defaults to the text column (an FAQ where one column IS the answer).
    """
    if not str(query or "").strip():
        raise KnowledgeError("a knowledge query is required")
    ds = await load_knowledge_dataset(db, dataset_id, owner_id)
    cols = _column_names(ds)
    if not cols:
        raise KnowledgeError(f"knowledge dataset {ds.name!r} has no schema columns")
    if text_column not in cols:
        raise KnowledgeError(f"knowledge dataset {ds.name!r} has no column "
                             f"{text_column!r} - columns: {', '.join(cols)}")
    answer_col = answer_column or text_column
    if answer_col not in cols:
        raise KnowledgeError(f"knowledge dataset {ds.name!r} has no answer column "
                             f"{answer_col!r} - columns: {', '.join(cols)}")
    if ds.row_count and not ds.file_path:
        raise KnowledgeError(f"knowledge dataset {ds.name!r} has no stored rows")
    path = ds_svc.datasets_dir() / ds.file_path
    if not ds_svc.file_exists(path):
        return {"matches": [], "query": query, "dataset": ds.name,
                "searched": 0, "note": "the dataset's rows are not on disk"}
    df = ds_svc.read_parquet_df(path)
    if text_column not in df.columns or answer_col not in df.columns:
        raise KnowledgeError(f"knowledge dataset {ds.name!r} is stored without the "
                             f"bound columns ({text_column!r}/{answer_col!r})")

    q_tokens = tokenize(query)
    matches: list[dict] = []
    for idx, row in enumerate(df.itertuples(index=False)):
        record = dict(zip(df.columns, row))
        question = str(record.get(text_column) or "")
        answer = str(record.get(answer_col) or "")
        score = score_match(q_tokens, tokenize(question))
        if score > 0.0:
            matches.append({"score": round(score, 6), "question": question,
                            "answer": answer, "row_index": idx})
    matches.sort(key=lambda m: (-m["score"], m["row_index"]))
    top_k = max(1, min(int(top_k or 1), 5))
    return {"matches": matches[:top_k], "query": query, "dataset": ds.name,
            "searched": int(len(df))}
