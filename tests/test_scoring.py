from app.scoring import WEIGHTS, issue_count, score


def v(impact, nodes=1):
    return {"id": f"rule-{impact}-{nodes}", "impact": impact, "nodes": [{}] * nodes}


def test_weights():
    assert WEIGHTS == {"critical": 15, "serious": 10, "moderate": 5, "minor": 2}


def test_score_counts_each_rule_once():
    assert score([v("critical", nodes=5)]) == 85
    assert score([v("critical"), v("serious"), v("moderate"), v("minor")]) == 100 - 32


def test_score_floors_at_zero():
    assert score([v("critical")] * 10) == 0


def test_clean_page_scores_100():
    assert score([]) == 100


def test_issue_count_sums_nodes():
    assert issue_count([v("critical", 3), v("minor", 2)]) == 5
