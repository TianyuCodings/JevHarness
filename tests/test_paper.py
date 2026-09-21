import copy
import pytest
from auto_jev import paper
from auto_jev.frozen import digest,source_hash
from auto_jev.observations import OBSERVATION_CONFIG
from auto_jev.spec import spec_hash
from auto_jev.providers import JevClient


def artifact():
    spec={'version':1,'name':'paper test','jev_model':'typesafe-ai/jev','nodes':[],'output':'1.0'}
    a={'version':1,'task_id':'crypto_spot','spec':spec,'spec_hash':spec_hash(spec),'source_hash':source_hash(),
       'observation_config':OBSERVATION_CONFIG.copy(),'costs':{'initial_cash':10000.,'fee_bps':10.,'slippage_bps':5.},'jev_metadata':{'transport':'mock'}}
    a['artifact_hash']=digest(a);return a


def test_paper_account_is_persistent_and_only_trades_once_per_bar(tmp_path,monkeypatch):
    now=1_800_000_123.;end=int(now)//3600*3600
    bars=[{'timestamp':end-(3-i)*3600,'open':100.,'high':100.,'low':100.,'close':100.,'volume':10.} for i in range(3)]
    monkeypatch.setattr(paper.time,'time',lambda:now)
    monkeypatch.setattr(paper,'fetch_coinbase',lambda *a,**k:{'bars':bars})
    monkeypatch.setattr(paper,'_ticker',lambda a:{'mid':100.,'received_at':now+1})
    path=tmp_path/'paper.json';frozen=artifact()
    first=paper.paper_step(frozen,JevClient(mock=True),state_path=path)
    assert first['state']['cash']==pytest.approx(0,abs=1e-10)
    assert first['state']['quantity']>0 and len(first['state']['trades'])==1
    assert first['state']['artifact_hash']==frozen['artifact_hash']
    second=paper.paper_step(frozen,JevClient(mock=True),state_path=path)
    assert second['skipped'] and len(second['state']['trades'])==1
    modified=copy.deepcopy(frozen);modified['costs']['fee_bps']=20;modified['artifact_hash']=digest({k:v for k,v in modified.items() if k!='artifact_hash'})
    with pytest.raises(ValueError,match='another artifact'):paper.paper_step(modified,JevClient(mock=True),state_path=path)
    modified=copy.deepcopy(frozen);modified['run_id']='another experiment';modified['artifact_hash']=digest({k:v for k,v in modified.items() if k!='artifact_hash'})
    with pytest.raises(ValueError,match='another artifact'):paper.paper_step(modified,JevClient(mock=True),state_path=path)


def test_news_revision_gets_its_own_arrival_time():
    original={'id':'id','headline':'first','content':'old','published_at':1}
    first=paper._merge_news([],[original],10)
    same=paper._merge_news(first,[original],20)
    assert same[0]['available_at']==10
    revised=paper._merge_news(same,[{**original,'content':'new'}],30)
    assert revised[0]['available_at']==30
    titled=paper._merge_news(revised,[{**original,'content':'new','headline':'revised title'}],40)
    assert titled[0]['version']==3 and titled[0]['available_at']==40
