"""Knowledge-base retrieval: embeddings plus authority bonus, keyword fallback."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path

AUTHORITY_BONUS = {"official-policy": 0.05, "help-center": 0.0, "legacy": -0.05}

Embedder = Callable[[list[str]], list[list[float]]]

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "is",
        "are",
        "be",
        "it",
        "if",
        "your",
        "you",
        "my",
        "i",
        "we",
        "our",
        "with",
        "from",
        "as",
        "that",
        "this",
        "can",
        "do",
        "does",
        "how",
        "what",
        "when",
    ]
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


EMBEDDINGS_TIMEOUT_S = 10.0
EMBEDDINGS_MAX_RETRIES = 1


def openai_embedder(model: str) -> Embedder:
    """Wrap the OpenAI embeddings API; the client is created on first call, with a
    short timeout so a slow embeddings call falls back to keywords quickly."""
    client = None

    def embed(texts: list[str]) -> list[list[float]]:
        nonlocal client
        if client is None:
            import openai

            client = openai.OpenAI(
                timeout=EMBEDDINGS_TIMEOUT_S, max_retries=EMBEDDINGS_MAX_RETRIES
            )
        resp = client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in resp.data]

    return embed


def _tokens(text: str) -> set[str]:
    out = set()
    for word in _TOKEN_RE.findall(text.lower()):
        if word in STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        out.add(word)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _article_text(article: dict) -> str:
    return article["title"] + "\n" + article["body"]


def _bonus(article: dict) -> float:
    return AUTHORITY_BONUS.get(article.get("authority", ""), 0.0)


def _drop_superseded_legacy(
    ranked: list[tuple[float, dict]],
) -> list[tuple[float, dict]]:
    """Drop a legacy article ranked below an official-policy article of the same
    category: official policy overrides legacy. With embeddings every article has a
    score, so "an official article matches" means it ranks above the legacy one."""
    official_seen: set[str | None] = set()
    kept = []
    for score, article in ranked:
        authority = article.get("authority")
        if authority == "legacy" and article.get("category") in official_seen:
            continue
        if authority == "official-policy":
            official_seen.add(article.get("category"))
        kept.append((score, article))
    return kept


class KnowledgeBase:
    def __init__(
        self,
        articles: list[dict],
        embed: Embedder | None,
        cache_path: Path | None,
        on_event: Callable[..., None] | None = None,
    ):
        self.articles = list(articles)
        self.embed = embed
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.on_event = on_event
        self._vectors: list[list[float]] | None = None
        self._cache: dict[str, list[float]] | None = None
        # Set on the first embeddings failure: later searches go straight to the
        # keyword fallback instead of waiting on a failing API every time.
        self._embeddings_failed = False

    @classmethod
    def load(
        cls,
        kb_path: Path,
        embed: Embedder | None,
        cache_path: Path | None,
        on_event: Callable[..., None] | None = None,
    ) -> KnowledgeBase:
        articles = json.loads(Path(kb_path).read_text(encoding="utf-8"))
        return cls(articles, embed=embed, cache_path=cache_path, on_event=on_event)

    def _emit(self, kind: str, **fields) -> None:
        if self.on_event is not None:
            self.on_event(kind, **fields)

    def _read_cache(self) -> dict[str, list[float]]:
        if self._cache is None:
            self._cache = {}
            if self.cache_path is not None and self.cache_path.exists():
                try:
                    self._cache = json.loads(
                        self.cache_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    self._cache = {}
        return self._cache

    def _article_vectors(self) -> list[list[float]]:
        if self._vectors is not None:
            return self._vectors
        cache = self._read_cache()
        texts = [_article_text(a) for a in self.articles]
        keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        missing = [i for i, k in enumerate(keys) if k not in cache]
        if missing:
            vectors = self.embed([texts[i] for i in missing])
            for i, vec in zip(missing, vectors):
                cache[keys[i]] = list(vec)
            if self.cache_path is not None:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(cache), encoding="utf-8")
        self._vectors = [cache[k] for k in keys]
        return self._vectors

    def _embedding_scores(self, query: str) -> list[tuple[float, dict]]:
        vectors = self._article_vectors()
        qvec = self.embed([query])[0]
        return [
            (_cosine(qvec, v) + _bonus(a), a) for a, v in zip(self.articles, vectors)
        ]

    def _keyword_scores(self, query: str) -> list[tuple[float, dict]]:
        qtokens = _tokens(query)
        if not qtokens:
            return []
        matches = []
        for a in self.articles:
            overlap = qtokens & _tokens(_article_text(a))
            if overlap:
                matches.append((len(overlap) / len(qtokens) + _bonus(a), a))
        official_categories = {
            a.get("category")
            for _, a in matches
            if a.get("authority") == "official-policy"
        }
        return [
            (s, a)
            for s, a in matches
            if not (
                a.get("authority") == "legacy"
                and a.get("category") in official_categories
            )
        ]

    def search(self, query: str, k: int = 3) -> dict:
        fallback = False
        scored: list[tuple[float, dict]]
        if self.embed is None or self._embeddings_failed:
            fallback = True
            scored = self._keyword_scores(query)
        else:
            try:
                scored = self._embedding_scores(query)
            except Exception as exc:  # noqa: BLE001 - any embedder failure falls back
                fallback = True
                self._embeddings_failed = True
                self._emit(
                    "error",
                    level="warning",
                    message="embeddings unavailable; keyword fallback",
                    error=str(exc),
                )
                scored = self._keyword_scores(query)

        scored.sort(key=lambda sa: sa[1]["id"])
        scored.sort(key=lambda sa: sa[1].get("last_updated", ""), reverse=True)
        scored.sort(key=lambda sa: sa[0], reverse=True)
        top = _drop_superseded_legacy(scored)[:k]
        articles = [
            {
                "id": a["id"],
                "title": a["title"],
                "authority": a.get("authority"),
                "last_updated": a.get("last_updated"),
                "body": a["body"],
                "score": round(s, 4),
            }
            for s, a in top
        ]
        self._emit(
            "kb_search",
            query=query,
            fallback=fallback,
            results=[
                {"id": a["id"], "score": a["score"], "authority": a["authority"]}
                for a in articles
            ],
        )
        return {"outcome": "results", "fallback": fallback, "articles": articles}
