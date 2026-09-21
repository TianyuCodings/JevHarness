"""Checks that the research dashboard preserves selection evidence."""
import pytest
from auto_jev.storage import RunStore


def test_matrix_preserves_tied_winners_and_failure_penalties(tmp_path):
    store=RunStore(tmp_path)
    run=store.create_run('test',{})
    for name in ('candidate_a','candidate_b'):
        store.save_candidate(run,{'name':name},name)
        store.append_evaluation(run,{'candidate':name,'episode_id':'window1','split':'validation','status':'ok','score':.1})
    store.append_evaluation(run,{'candidate':'candidate_a','episode_id':'window2','split':'validation','status':'error','score':-2.})
    matrix=store.matrix(run)
    assert set(matrix['champions']['window1'])=={'candidate_a','candidate_b'}
    row=next(r for r in matrix['rows'] if r['hash']=='candidate_a')
    assert row['mean_score']==pytest.approx(-.95)


def test_data_kind_false_synthetic_flag_is_real():
    from auto_jev.storage import episode_data_kind
    assert episode_data_kind({'provenance':{'synthetic':False,'source':'Coinbase Exchange'}})=='real'
