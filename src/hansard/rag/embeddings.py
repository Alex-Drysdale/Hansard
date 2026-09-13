"""Local embeddings.

Runs entirely on this machine: fastembed ships ONNX weights, so there is no
torch install, no GPU and no API call. The model downloads once and is cached.

Two things here were discovered the hard way and are worth keeping in mind.

**fastembed's ``query_embed`` does not apply a query prefix.** It is an alias
for ``embed`` for every model tried here, despite these models being trained
with an asymmetry between a stored passage and a question about it. Assuming
the library handles it silently costs retrieval quality with no error, so the
prefix is applied explicitly below.

**Model choice is constrained by chunk size, not by benchmark scores.**
``all-MiniLM-L6-v2`` embeds ten times faster than the alternatives -- because
fastembed truncates it at 128 tokens, roughly 90 words. Our chunks average 162
words and reach 350, so more than half of most passages would be silently
discarded. Nothing errors; retrieval just quietly gets worse.
:func:`check_model_fits_chunks` turns that trap into a loud failure.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import lru_cache

from hansard.logging_config import get_logger

log = get_logger(__name__)

#: The instruction these models were trained to see on the query side. Applied
#: by hand because fastembed does not, despite the method name suggesting it.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

#: Tokens per word for parliamentary prose, measured rather than guessed: over
#: 800 real chunks the mean is 1.24, p95 is 1.35 and the worst is 1.65. 1.4
#: leaves headroom above the 95th percentile without being so pessimistic that
#: it rejects a chunk size which measurement shows is fine -- an earlier 1.6
#: guess did exactly that, refusing 350-word chunks that in fact reach only 450
#: of the 512 tokens available.
TOKENS_PER_WORD = 1.4


class EmbeddingConfigError(RuntimeError):
    """The embedding model cannot hold the configured chunk size."""


@lru_cache(maxsize=4)
def _model(name: str):  # type: ignore[no-untyped-def]
    """Load and cache the embedding model.

    Cached because construction memory-maps the weights, and the web server
    would otherwise pay that on every request.
    """
    from fastembed import TextEmbedding

    log.info("embeddings.loading", model=name)
    return TextEmbedding(model_name=name)


def max_tokens(model_name: str) -> int | None:
    """The model's truncation limit, read from its tokenizer.

    Read rather than hard-coded: the limit fastembed applies is not always the
    one the model card advertises, which is exactly how the MiniLM trap works.
    """
    try:
        truncation = _model(model_name).model.tokenizer.truncation
    except AttributeError:  # pragma: no cover - depends on fastembed internals
        return None
    return int(truncation["max_length"]) if truncation else None


def check_model_fits_chunks(model_name: str, chunk_words: int) -> None:
    """Fail loudly when the model would silently truncate our chunks."""
    limit = max_tokens(model_name)
    if limit is None:
        return
    needed = int(chunk_words * TOKENS_PER_WORD)
    if needed > limit:
        raise EmbeddingConfigError(
            f"{model_name} truncates at {limit} tokens, but chunks of "
            f"{chunk_words} words need roughly {needed}. The tail of every long "
            f"passage would be dropped without any error. Either lower "
            f"HANSARD_CHUNK_WORDS to about {int(limit / TOKENS_PER_WORD)} or "
            f"choose a model with a longer context."
        )


def dimensions(model_name: str) -> int:
    """Vector width, discovered from the model rather than hard-coded."""
    vector = next(iter(_model(model_name).embed(["dimension probe"])))
    return len(vector)


def embed_passages(
    texts: Sequence[str], *, model_name: str, parallel: int | None = None
) -> list[list[float]]:
    """Embed stored text.

    No prefix: these are the documents being searched, not the question.

    ``parallel`` spreads batches across processes. Worth roughly 3x on this
    machine; the workers re-import the entry module, which is safe because both
    the console script and ``python -m`` guard their top-level call.
    """
    if not texts:
        return []
    vectors = _model(model_name).embed(list(texts), batch_size=32, parallel=parallel)
    return [vector.tolist() for vector in vectors]


def embed_query(text: str, *, model_name: str) -> list[float]:
    """Embed a question, with the instruction prefix the model expects."""
    prefixed = QUERY_PREFIX + text
    vector = next(iter(_model(model_name).embed([prefixed])))
    return list(vector.tolist())


def batched(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
