import json
from auto_jev.data import append_received_news
from auto_jev.observations import build_observation


def test_rss_polling_keeps_first_arrival_and_separate_revisions(tmp_path):
    path=tmp_path/'news.jsonl'
    item={'id':'story','source':'feed','headline':'A','content':'body','available_at':10}
    assert append_received_news(path,[item])==1
    assert append_received_news(path,[{**item,'available_at':20}])==0
    assert append_received_news(path,[{**item,'headline':'B','available_at':30}])==1
    assert [r['available_at'] for r in map(json.loads,path.read_text().splitlines())]==[10,30]


def test_recent_revision_survives_window_and_preserves_truncation():
    news=[{'id':str(i),'available_at':i,'content':'text'} for i in range(25)]
    news.append({'id':'0','available_at':30,'content':'cut','input_truncated':True})
    obs=build_observation('BTC-USD',[{'close':100}],news,{'cash':1,'quantity':0,'equity':1},40)
    assert obs['news'][-1]['id']=='0'
    assert obs['news'][-1]['input_truncated']
