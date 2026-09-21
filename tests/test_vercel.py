"""Contract tests based on a real Gateway evaluation response, without network."""
import copy
import json
import httpx
import pytest
from auto_jev import vercel

KEY='test-key-never-log'
QUESTIONS={
    'on':{'type':'noul','instructions':'Is the lamp on?'},
    'color':{'type':'choice','instructions':'What color?','criteria':{'red':'red','blue':'blue'}},
    'brightness':{'type':'score','instructions':'How bright?','criteria':['off','dim','bright']},
}
RESPONSE={
    'answers':{'on':{'type':'boolean','probability':.99},'color':{'type':'choice','choice':'red','probabilities':{'blue':0,'red':1}},'brightness':{'type':'score','score':2,'probabilities':{'0':0,'1':0,'2':1}}},
    'usage':{'inputTokens':364,'outputTokens':61},
    'rounding':{'probabilityDecimals':2,'scoreDecimals':2},
    'providerMetadata':{'typesafe':{'confidence':{'color':1,'brightness':1}},'gateway':{'cost':'0'}},
}

def call(response=RESPONSE, *, status=200, questions=QUESTIONS):
    requests=[]
    def handler(r):
        requests.append(r)
        return httpx.Response(status,content=json.dumps(response),headers={"content-type":"application/json"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=vercel.vercel_judge(KEY,'The red lamp is bright.',questions,client=client,max_retries=0)
    return result, requests


def test_real_response_contract_and_request_routing():
    result,requests=call()
    req=requests[0]
    assert str(req.url)==vercel.VERCEL_EVALUATION_URL
    assert req.headers['authorization']=='Bearer '+KEY
    assert req.headers['ai-model-id']=='typesafe-ai/jev'
    body=json.loads(req.content)
    assert set(body)=={'state','questions'} and body['questions']['on']['type']=='boolean'
    assert result['answers']['on']=={'type':'noul','noul':.99}
    assert result['answers']['color']['choice']=='red'
    assert result['answers']['color']['confidence']==1
    assert result['answers']['brightness']['score']==2
    assert result['rounding']==RESPONSE['rounding']
    assert result['gateway_metadata']['model_version_pinned'] is False
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize('mutation',[
    lambda d:d['answers'].pop('on'),
    lambda d:d['answers']['color'].pop('choice'),
    lambda d:d['answers']['color'].update(choice='green'),
    lambda d:d['answers']['brightness'].update(score=7.5),
    lambda d:d['answers']['brightness'].update(probabilities={'1':.5,'5':.5}),
    lambda d:d['answers']['on'].update(probability=float('nan')),
    lambda d:d['answers']['on'].pop('type'),
])
def test_malformed_answers_are_not_silently_repaired(mutation):
    response=copy.deepcopy(RESPONSE);mutation(response)
    with pytest.raises(vercel.VercelError):call(response)


def test_rounding_allows_point_99_sum_without_normalizing():
    response=copy.deepcopy(RESPONSE)
    response['answers']['brightness'].update(score=.99,probabilities={'0':.33,'1':.33,'2':.33})
    result,_=call(response)
    assert result['answers']['brightness']['probabilities']=={'0':.33,'1':.33,'2':.33}


@pytest.mark.parametrize('status',[401,403,307])
def test_auth_and_redirects_fail_without_leaking_secrets(status):
    with pytest.raises(vercel.VercelError) as err:call({'error':{'message':KEY}},status=status)
    assert KEY not in str(err.value)


def test_retry_429_but_not_auth(monkeypatch):
    seen=[];monkeypatch.setattr(vercel,'_sleep',lambda _:None)
    def handler(req):
        seen.append(req)
        return httpx.Response(429 if len(seen)==1 else 200,json=RESPONSE)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        vercel.vercel_judge(KEY,'test',QUESTIONS,client=client)
        assert not client.is_closed
    assert len(seen)==2


def test_request_schema_is_checked_before_network():
    questions=copy.deepcopy(QUESTIONS);questions['color'].pop('criteria')
    with pytest.raises(vercel.VercelError):call(questions=questions)
