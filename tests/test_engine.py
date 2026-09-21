import json
import pytest
from auto_jev.evolution import run_evolution,freeze_run
from auto_jev.frozen import evaluate_frozen
from auto_jev.providers import JevClient
from auto_jev.runtime import PipelineRuntime
from auto_jev.spec import spec_hash
from auto_jev.storage import RunStore


def spec(output='0'):
    return {'version':1,'name':'classifier','jev_model':'typesafe-ai/jev','nodes':[],'output':output}


def test_non_trading_task_runs_native_gepa_and_freezes(tmp_path):
    train=[{'id':'train','input':{'x':1},'label':1}]
    val=[{'id':'validation','input':{'x':2},'label':1}]
    prompts=[]
    def proposer(prompt):
        prompts.append(prompt)
        assert 'SECRET_HELD_OUT' not in prompt
        return json.dumps(spec('1'))
    def evaluator(pipeline,episode,jev,capture_traces=True):
        prediction=PipelineRuntime(pipeline,jev).run(episode['input'])['output']
        return {'score':float(prediction==episode['label']),'prediction':prediction,'traces':[]}
    store=RunStore(tmp_path)
    result=run_evolution(train,val,jev=JevClient(mock=True),proposer=proposer,store=store,
                        max_metric_calls=4,seed_pipeline=spec(),evaluator=evaluator,task_id='classification')
    assert prompts and result['best_score']==1
    assert store.list_events(result['run_id'],'parent_selected')
    artifact=freeze_run(store,result['run_id'])
    held_out=[{'id':'SECRET_HELD_OUT','input':{'x':9},'label':1}]
    evaluated=evaluate_frozen(artifact,held_out,JevClient(mock=True),evaluator=evaluator)
    assert evaluated[0]['score']==1
    assert 'SECRET_HELD_OUT' not in json.dumps(store.get_run(result['run_id']))
    artifact['costs']['changed']=True
    with pytest.raises(ValueError,match='integrity'):evaluate_frozen(artifact,held_out,JevClient(mock=True),evaluator=evaluator)


def test_cache_namespace_integrity_and_credential_routing(tmp_path):
    with pytest.raises(ValueError,match='explicit transport'):
        JevClient(api_key='a-vercel-key',transport='auto')
    question={'q':{'type':'noul','instructions':'Is this positive?'}}
    first=JevClient(mock=True,cache_dir=tmp_path,cache_namespace='recorded')
    first.judge({'closes':[1,2]},question)
    replay=JevClient(mock=True,cache_dir=tmp_path,cache_namespace='recorded',cache_only=True)
    assert replay.judge({'closes':[1,2]},question)['cache_hit']
    assert replay.metadata['actual_model']=='mock-engineering-only'
    fresh=JevClient(mock=True,cache_dir=tmp_path,cache_namespace='fresh',cache_only=True)
    with pytest.raises(RuntimeError,match='cache-only'):fresh.judge({'closes':[1,2]},question)
    cache_file=next((tmp_path/'recorded').glob('*.json'))
    content=json.loads(cache_file.read_text());content['answers']['q']['noul']=.99
    cache_file.write_text(json.dumps(content))
    with pytest.raises(RuntimeError,match='integrity'):replay.judge({'closes':[1,2]},question)
