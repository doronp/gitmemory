"""Benchmark retriever arms factories."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

from gitmemory import index, store
from gitmemory.adapters import claude_code as cc

_MODEL = None
_RANKER = None


class GitMemoryRetriever:
    """A gitmemory store and index built for one evaluation instance.

    `close()` and not `__del__`: a sweep over the full corpus builds one of
    these per instance per compaction mode, and `__del__` runs whenever the
    interpreter gets round to it — which on a reference cycle is never. Each
    one holds an open SQLite handle and a temp directory containing a copy of
    the transcript, so "eventually" is a file-descriptor ceiling and a disk
    full of stores. `score` closes every arm it builds in a `finally`. [E3]
    """

    def __init__(self, temp_home: str, db, retriever_func):
        self.temp_home = temp_home
        self.db = db
        self.retriever_func = retriever_func

    def __call__(self, query: str, k: int) -> list[int]:
        return self.retriever_func(query, k)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()
        shutil.rmtree(self.temp_home, ignore_errors=True)


def gitmemory_factory(
    instance,
    transcript_bytes: bytes,
    *,
    weights: index.Weights = index.DEFAULT_WEIGHTS,
) -> GitMemoryRetriever:
    """Exposes the candidate gitmemory index retriever arm.

    `weights` is threaded through because `index.Weights` documents itself as "a
    starting point, not a result: `bench/` is what replaces them with measured
    ones", and a sweep that can only ever run `DEFAULT_WEIGHTS` cannot do that
    job. `python -m bench --weights` is the caller. [E3]
    """
    temp_home = tempfile.mkdtemp()
    src_file = os.path.join(temp_home, "source.jsonl")
    with open(src_file, "wb") as f:
        f.write(transcript_bytes)

    store.capture(src_file, "claude-code", instance.question_id, home=temp_home)
    index.build(temp_home)
    db = index.open_db(index.db_path(temp_home))
    return GitMemoryRetriever(temp_home, db, index.retriever(db, weights=weights))


def get_model():
    """Lazily loads and caches the model2vec StaticModel."""
    global _MODEL
    if _MODEL is None:
        from model2vec import StaticModel

        _MODEL = StaticModel.from_pretrained("minishlab/potion-base-8M")
    return _MODEL


def get_ranker():
    """Lazily loads and caches the flashrank Ranker."""
    global _RANKER
    if _RANKER is None:
        from flashrank import Ranker

        _RANKER = Ranker()
    return _RANKER


def dense_factory(instance, transcript_bytes: bytes):
    """Exposes the dense retriever arm using model2vec."""
    try:
        import numpy as np
        from model2vec import StaticModel  # noqa: F401
    except ImportError as e:
        raise ImportError(f"Dense arm dependencies not installed: {e}") from e

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp.write(transcript_bytes)
        tmp_path = tmp.name
    try:
        parsed = cc.parse(tmp_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(tmp_path).unlink()

    turns_with_text = []
    texts = []
    for turn in parsed.turns:
        text = "".join(b.text for b in turn.blocks).strip()
        if text:
            turns_with_text.append(turn)
            texts.append(text)

    if not texts:
        return lambda query, k: []

    model = get_model()
    turn_embeddings = model.encode(texts)

    def retrieve(query: str, k: int) -> list[int]:
        if not query.strip() or not texts:
            return []
        query_emb = model.encode([query])[0]
        norm_turns = np.linalg.norm(turn_embeddings, axis=1)
        norm_query = np.linalg.norm(query_emb)
        if norm_query == 0:
            return []

        # Handle any zero norms in turns safely
        norm_turns_safe = np.where(norm_turns == 0, 1e-9, norm_turns)
        similarities = np.dot(turn_embeddings, query_emb) / (norm_turns_safe * norm_query)
        # `kind="stable"`: numpy's default introsort breaks ties by whatever the
        # partition happened to do, so two turns with equal cosine similarity can
        # swap ranks between runs and move MRR. A benchmark whose numbers are not
        # reproducible cannot be a gate. [E3]
        top_indices = np.argsort(-similarities, kind="stable")[:k]
        return [turns_with_text[idx].byte_offset for idx in top_indices]

    return retrieve


FIRST_STAGE_DEPTH = 50  # candidates the reranker is allowed to reorder


class RerankRetriever:
    """BM25 first stage, flashrank second. Owns the first stage's store."""

    def __init__(self, first_stage: GitMemoryRetriever, retrieve_func):
        self.first_stage = first_stage
        self.retrieve_func = retrieve_func

    def __call__(self, query: str, k: int) -> list[int]:
        return self.retrieve_func(query, k)

    def close(self) -> None:
        self.first_stage.close()


def rerank_factory(instance, transcript_bytes: bytes, *, depth: int = FIRST_STAGE_DEPTH):
    """Exposes the reranking retriever arm using flashrank."""
    try:
        from flashrank import Ranker, RerankRequest  # noqa: F401
    except ImportError as e:
        raise ImportError(f"Rerank arm dependencies not installed: {e}") from e

    # rerank relies on first-stage candidate retrieval and flashrank
    first_stage = gitmemory_factory(instance, transcript_bytes)

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp.write(transcript_bytes)
        tmp_path = tmp.name
    try:
        parsed = cc.parse(tmp_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(tmp_path).unlink()

    turn_map = {}
    for turn in parsed.turns:
        text = "".join(b.text for b in turn.blocks).strip()
        turn_map[turn.byte_offset] = text

    def retrieve(query: str, k: int) -> list[int]:
        first = first_stage(query, depth)
        if not first:
            return []

        passages = [{"id": offset, "text": turn_map.get(offset, "")} for offset in first]
        ranker = get_ranker()
        results = ranker.rerank(RerankRequest(query=query, passages=passages))
        return [res["id"] for res in results[:k]]

    return RerankRetriever(first_stage, retrieve)
