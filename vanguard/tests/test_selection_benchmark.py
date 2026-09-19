from datetime import datetime,timedelta,timezone

from model.score_directional_swing import choose_complete_bar, FINAL_COVERAGE_SQL
from research.selection_benchmark import choose,metrics


def test_sparse_final_bar_does_not_displace_broad_snapshot():
    latest=datetime(2026,9,18,9,45,tzinfo=timezone.utc)
    wide={'ts':latest-timedelta(minutes=30),'candidates':210,'covered':208}
    assert choose_complete_bar([{'ts':latest,'candidates':61,'covered':14},wide]) == wide


def test_latest_complete_bar_wins_and_small_or_stale_cohorts_fail_closed():
    latest=datetime(2026,9,18,9,45,tzinfo=timezone.utc)
    assert choose_complete_bar([{'ts':latest,'candidates':210,'covered':170}])['ts']==latest
    assert choose_complete_bar([{'ts':latest,'candidates':210,'covered':14}]) is None
    assert choose_complete_bar([{'ts':latest,'candidates':6,'covered':6}]) is None
    assert choose_complete_bar([]) is None
    assert "head.ts - INTERVAL '1 hour'" in FINAL_COVERAGE_SQL
    assert "count(DISTINCT o.option_type)=2" in FINAL_COVERAGE_SQL
    assert "::date=(head.ts" in FINAL_COVERAGE_SQL


def test_hindsight_returns_and_missing_outcomes_cannot_change_selection():
    rows=[{'symbol':'A','option_type':'CE','ranking_score':.9,'return':None},
          {'symbol':'B','option_type':'PE','ranking_score':.8,'return':-1},
          {'symbol':'C','option_type':'PE','ranking_score':.7,'return':10}]
    assert [r['symbol'] for r in choose(rows,limit=2)]==['A','B']
    result=metrics(choose(rows,limit=2))
    assert result['selected']==2 and result['resolved']==1 and result['wins']==0


def test_net_hit_rate_uses_cost_and_does_not_zero_fill_missing():
    result=metrics([{'option_type':'CE','return':.005},
                    {'option_type':'PE','return':.02},
                    {'option_type':'PE','return':None}])
    assert result['resolved']==2 and result['wins']==1 and result['hit_rate']==.5
