from pathlib import Path

from kb import KnowledgeBase

KB_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "knowledge-base" / "articles.json"
)


def art(id_, authority, category="shared"):
    return {
        "id": id_,
        "title": f"Title {id_}",
        "category": category,
        "body": f"Body {id_}",
        "last_updated": "2026-01-01",
        "authority": authority,
    }


def similarity_embedder(sims):
    """Stub embedder: the query embeds as [1, 0]; article `id` embeds at cosine
    `sims[id]` from it."""

    def embed(texts):
        out = []
        for t in texts:
            id_ = t.removeprefix("Title ").split("\n")[0]
            s = sims.get(id_, 1.0)
            out.append([s, (1 - s * s) ** 0.5])
        return out

    return embed


def test_authority_bonus_orders_equal_similarity():
    arts = [
        art("a", "legacy", "x"),
        art("b", "help-center", "y"),
        art("c", "official-policy", "z"),
    ]
    kb = KnowledgeBase(
        arts, embed=lambda texts: [[1.0, 0.0]] * len(texts), cache_path=None
    )
    assert [a["id"] for a in kb.search("x")["articles"]] == ["c", "b", "a"]


def test_embeddings_drop_legacy_below_same_category_official():
    arts = [
        art("leg", "legacy", "fees"),
        art("off", "official-policy", "fees"),
        art("help1", "help-center", "fees"),
        art("help2", "help-center", "other"),
    ]
    sims = {"leg": 0.80, "off": 0.82, "help1": 0.70, "help2": 0.60}
    kb = KnowledgeBase(arts, embed=similarity_embedder(sims), cache_path=None)
    ids = [a["id"] for a in kb.search("query")["articles"]]
    assert ids == ["off", "help1", "help2"]


def test_embeddings_keep_legacy_ranked_above_official():
    arts = [art("leg", "legacy", "fees"), art("off", "official-policy", "fees")]
    sims = {"leg": 0.99, "off": 0.50}
    kb = KnowledgeBase(arts, embed=similarity_embedder(sims), cache_path=None)
    ids = [a["id"] for a in kb.search("query")["articles"]]
    assert ids == ["leg", "off"]


def test_embeddings_keep_legacy_without_official_in_category():
    arts = [art("leg", "legacy", "fees"), art("off", "official-policy", "other")]
    sims = {"leg": 0.80, "off": 0.82}
    kb = KnowledgeBase(arts, embed=similarity_embedder(sims), cache_path=None)
    ids = [a["id"] for a in kb.search("query")["articles"]]
    assert ids == ["off", "leg"]


def test_keyword_fallback_grace_question():
    kb = KnowledgeBase.load(KB_PATH, embed=None, cache_path=None)
    out = kb.search("grace period for late returns")
    ids = [a["id"] for a in out["articles"]]
    assert out["fallback"] and ids and "kb_ext_01" in ids and "kb_fee_01" not in ids


def test_fallback_on_embedder_error_logs():
    events = []

    def boom(texts):
        raise RuntimeError("no network")

    def on_event(kind, **fields):
        events.append((kind, fields))

    kb = KnowledgeBase.load(KB_PATH, embed=boom, cache_path=None, on_event=on_event)
    out = kb.search("grace period for late returns")
    assert out["fallback"] is True
    kinds = [k for k, _ in events]
    assert "error" in kinds and "kb_search" in kinds


def test_embedder_failure_remembered():
    calls = []

    def boom(texts):
        calls.append(texts)
        raise RuntimeError("no network")

    kb = KnowledgeBase.load(KB_PATH, embed=boom, cache_path=None)
    assert kb.search("grace period")["fallback"] is True
    assert kb.search("late fee")["fallback"] is True
    assert len(calls) == 1


def test_openai_embedder_uses_short_timeout(monkeypatch):
    import sys
    import types

    created = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.embeddings = types.SimpleNamespace(
                create=lambda model, input: types.SimpleNamespace(
                    data=[types.SimpleNamespace(embedding=[1.0]) for _ in input]
                )
            )

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    from kb import openai_embedder

    assert openai_embedder("m")(["a"]) == [[1.0]]
    assert created == {"timeout": 10.0, "max_retries": 1}


def test_cache_reused(tmp_path):
    cache = tmp_path / "kb_embeddings.json"
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return [[1.0, float(i)] for i in range(len(texts))]

    arts = [art("a", "legacy"), art("b", "help-center"), art("c", "official-policy")]
    KnowledgeBase(arts, embed=embed, cache_path=cache).search("x")
    assert len([n for n in calls if n > 1]) == 1
    KnowledgeBase(arts, embed=embed, cache_path=cache).search("x")
    assert len([n for n in calls if n > 1]) == 1


def test_results_carry_metadata():
    kb = KnowledgeBase.load(KB_PATH, embed=None, cache_path=None)
    out = kb.search("extend my rental")
    assert out["articles"]
    for a in out["articles"]:
        for key in ("id", "title", "authority", "last_updated", "body"):
            assert key in a
