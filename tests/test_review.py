"""Independent regression checks for profit accounting and time boundaries."""
import math
import pytest

from auto_jev.crypto import evaluate_episode
from auto_jev.providers import JevClient
from auto_jev.runtime import PipelineRuntime
from auto_jev.spec import validate_spec, evaluate_expression


def constant_spec(target):
    return {'version': 1, 'name': 'constant', 'jev_model': 'jev-1.13.0', 'nodes': [], 'output': str(target)}


def flat_episode():
    return {'id': 'flat', 'asset': 'BTC-USD', 'interval_seconds': 3600,
            'bars': [{'timestamp': i * 3600, 'open': 100., 'high': 100., 'low': 100., 'close': 100., 'volume': 10.} for i in range(4)],
            'news': [], 'provenance': {'synthetic': True}}


def test_round_trip_costs_are_paid_without_borrowing():
    fee, slip, cash = 0.001, 0.0005, 10000.
    result = evaluate_episode(constant_spec(1), flat_episode(), JevClient(mock=True), initial_cash=cash, fee_bps=fee * 10000, slippage_bps=slip * 10000)
    expected = cash * (1-slip) * (1-fee) / ((1+slip) * (1+fee))
    assert result['final_equity'] == pytest.approx(expected, abs=1e-6)
    assert result['net_profit'] == pytest.approx(expected-cash, abs=1e-6)
    assert result['max_drawdown_pct'] > 0


def test_cash_has_no_fictitious_return_or_transaction_cost():
    result = evaluate_episode(constant_spec(0), flat_episode(), JevClient(mock=True))
    assert result['net_profit'] == pytest.approx(0)
    assert result['fees_paid'] == pytest.approx(0)


def test_runtime_does_not_require_reflection_credentials(monkeypatch):
    for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'AI_GATEWAY_API_KEY', 'TYPESAFE_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    result = PipelineRuntime(constant_spec(.25), JevClient(mock=True)).run({'arbitrary': 'non-trading observation'})
    assert result['output'] == .25


@pytest.mark.parametrize('expression', ["__import__('os')", "obs.__class__", "open('/etc/passwd')", "(lambda: 1)()"])
def test_dsl_rejects_code_execution(expression):
    with pytest.raises((ValueError, TypeError, RuntimeError)):
        evaluate_expression(expression, {'obs': {}, 'nodes': {}, 'memory': {}})


def test_baselines_and_partial_position_accounting():
    from auto_jev.crypto import evaluate_baselines, _Book
    results=evaluate_baselines(flat_episode())
    assert set(results)=={'cash','buy_hold','momentum'}
    book=_Book(10000.,.1,.1)
    book.rebalance(.5,100.,0)
    assert book.qty*100/book.equity(100)==pytest.approx(.5)
    small=_Book(10000.,.001,.0005)
    small.rebalance(.001,100.,0)
    assert small.qty>0


@pytest.mark.parametrize('expression',['10 ** (10 ** 10)','[0] * 1000000000',"'%1000000000s' % 1"])
def test_resource_exhaustion_expressions_are_rejected(expression):
    with pytest.raises(ValueError):evaluate_expression(expression,{'obs':{},'nodes':{},'memory':{}})


def test_future_data_and_news_revisions_do_not_change_earlier_inputs():
    import copy
    episode=flat_episode()
    episode['news']=[{'id':'story','available_at':1000,'published_at':0,'headline':'original','content':'old'},
                     {'id':'story','available_at':8000,'published_at':0,'headline':'revision','content':'new'}]
    p=constant_spec(.5)
    before=evaluate_episode(p,episode,JevClient(mock=True))
    changed=copy.deepcopy(episode);changed['bars'][-1]['close']=120;changed['bars'][-1]['high']=120
    changed['news'].append({'id':'future','available_at':20000,'published_at':0,'headline':'future','content':'unavailable'})
    after=evaluate_episode(p,changed,JevClient(mock=True))
    assert [t['obs'] for t in before['traces']]==[t['obs'] for t in after['traces']]
    assert before['traces'][0]['obs']['news'][0]['headline']=='original'
    assert before['traces'][-1]['obs']['news'][0]['headline']=='revision'
