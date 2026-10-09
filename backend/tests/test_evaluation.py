import math
from collections import Counter

import pytest

from app.evaluation import dataset
from app.evaluation.harness import CORPUS, assert_disposable
from app.evaluation.metrics import bootstrap_ci, hit_at_k, mrr, ndcg_at_k, recall_at_k
from app.evaluation.retrieval_eval import supported, sweep_dev
from app.knowledge.chunker import chunk_pages
from app.knowledge.pdf import extract_pages


def test_metric_known_values():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"b", "z"}, 3) == 0.5
    assert hit_at_k(ranked, {"d"}, 3) == 0.0 and hit_at_k(ranked, {"d"}, 4) == 1.0
    assert mrr(ranked, {"c"}) == pytest.approx(1 / 3) and mrr(ranked, {"zz"}) == 0.0
    # perfect ordering -> nDCG 1; reversed grades -> < 1
    grades = {"a": 2, "b": 1}
    assert ndcg_at_k(["a", "b", "c"], grades, 3) == pytest.approx(1.0)
    worse = ndcg_at_k(["b", "a", "c"], grades, 3)
    expected = ((2 ** 1 - 1) / math.log2(2) + (2 ** 2 - 1) / math.log2(3)) / ((2 ** 2 - 1) / math.log2(2) + (2 ** 1 - 1) / math.log2(3))
    assert worse == pytest.approx(expected) and worse < 1
    assert ndcg_at_k(["x"], {}, 3) == 0.0 and recall_at_k([], set(), 3) == 0.0


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    vals = [0.0, 1.0, 1.0, 1.0, 0.0, 1.0]
    a, b = bootstrap_ci(vals), bootstrap_ci(vals)
    assert a == b and a[1] <= a[0] <= a[2]
    assert bootstrap_ci([0.5]) == (0.5, 0.5, 0.5) and math.isnan(bootstrap_ci([])[0])


@pytest.fixture(scope="module")
def corpus_chunks() -> dict[str, str]:
    chunks = {}
    for pdf, title in CORPUS:
        for c in chunk_pages(extract_pages(pdf.read_bytes(), 200), 120, 20):
            chunks[f"{title}#{c.chunk_index}"] = c.text
    return chunks


def test_dataset_is_valid_stratified_and_leak_free():
    qs = dataset.load_questions()
    assert len(qs) == 54
    counts = Counter((q.split, q.category) for q in qs)
    for cat in dataset.CATEGORIES:   # stratified: same number of each category in dev and test
        assert counts[("dev", cat)] == counts[("test", cat)] > 0
    assert len({q.question.strip().lower() for q in qs}) == len(qs)       # no question appears in both splits
    assert {q.id for q in qs if q.split == "dev"}.isdisjoint({q.id for q in qs if q.split == "test"})


def test_every_gold_phrase_resolves_to_a_chunk(corpus_chunks):
    for q in dataset.load_questions():
        if q.category == "answerable":
            grades = dataset.resolve_gold(q, corpus_chunks)
            assert grades and max(grades.values()) == 2, q.id


def test_unresolvable_gold_phrase_fails_loudly(corpus_chunks):
    bad = dataset.Question("x", "dev", "answerable", "q?", [{"phrase": "this phrase is nowhere", "grade": 2}])
    with pytest.raises(dataset.DatasetError):
        dataset.resolve_gold(bad, corpus_chunks)


def test_validation_rejects_bad_labels():
    good = dataset.load_questions()
    with pytest.raises(dataset.DatasetError):
        dataset.validate(good + [dataset.Question(good[0].id, "dev", "ambiguous", "dup id", [])])
    with pytest.raises(dataset.DatasetError):
        dataset.validate(good + [dataset.Question("zz", "dev", "answerable", "no gold", [])])
    with pytest.raises(dataset.DatasetError):
        dataset.validate(good + [dataset.Question("zz", "test", "ambiguous", good[0].question, [])])   # text leak


def test_support_function_and_threshold_sweep_guard():
    rec = {"n_terms": 5, "ranked": [{"matched_terms": 2}, {"matched_terms": 4}]}
    assert supported(rec, 4, 1) and not supported(rec, 4, 2) and supported(rec, 2, 2)
    assert supported({"n_terms": 1, "ranked": [{"matched_terms": 1}]}, 4, 1)   # short query: need is capped by its length
    assert supported({"n_terms": 3, "ranked": [{"matched_terms": 0, "dense": 0.8}]}, 2, 1, min_sim=0.7)
    with pytest.raises(AssertionError):       # sweeps are dev-only by construction
        sweep_dev([{"split": "test", "category": "answerable", "n_terms": 1, "ranked": []}], [None])


def test_eval_refuses_non_disposable_database():
    with pytest.raises(SystemExit):
        assert_disposable("postgresql://u:p@h/eduos")
    assert_disposable("postgresql://u:p@h/eduos_eval_test")
