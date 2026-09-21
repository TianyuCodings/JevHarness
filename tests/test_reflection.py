import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from auto_jev.evolution import run_evolution
from auto_jev.providers import JevClient
from auto_jev.reflection import ReflectionContextError, full_feedback
from auto_jev.storage import RunStore


def candidate(output='0'):
    return {'version':2,'name':'reflection test','jev_model':'typesafe-ai/jev','nodes':[],'output':output}


def detailed_result(ep, score):
    return {'score':score,'episode_id':ep['id'],
            'traces':[{'timestamp':i,'obs':{'text':'LONG_TEXT_'+('中文'*1700),'values':list(range(30))},
                       'trace':[{'id':f'node{j}','status':'ok','output':[i,j,'MIDDLE_NODE_DATA']} for j in range(12)]}
                      for i in range(14)],
            'trades':[{'trade':i,'fee':i/100} for i in range(15)],
            'equity_curve':[{'equity':100+i} for i in range(16)]}


def test_every_training_episode_and_every_trace_reach_proposer_unchanged(tmp_path):
    train=[{'id':f'train-{i}','label':1} for i in range(3)]
    validation=[{'id':'NEVER_SEND_VALIDATION_TO_REFLECTION','label':1}]
    prompts=[]
    def proposer(prompt):
        prompts.append(prompt)
        assert 'NEVER_SEND_VALIDATION_TO_REFLECTION' not in prompt
        data=json.loads(prompt)
        records=data['training_feedback']['pipeline']
        assert {r['episode_id'] for r in records}=={e['id'] for e in train}
        for record in records:
            assert record['episode']==next(e for e in train if e['id']==record['episode_id'])
            assert record['result']==detailed_result(record['episode'],0.)
        return json.dumps(candidate('1'))
    def evaluator(spec, ep, jev, capture_traces=True):
        return detailed_result(ep,float(spec['output']=='1'))
    store=RunStore(tmp_path)
    result=run_evolution(train,validation,jev=JevClient(mock=True),proposer=proposer,
                         evaluator=evaluator,task_id='test',seed_pipeline=candidate(),store=store,max_metric_calls=7)
    assert result['best_score']==1 and len(prompts)==1
    event=store.list_events(result['run_id'],'reflection_input')[0]['payload']
    archived=(store.run_dir(result['run_id'])/event['path']).read_text()
    assert archived==prompts[0]
    assert event['sha256']==hashlib.sha256(prompts[0].encode()).hexdigest()
    assert event['trace_count']==42 and event['node_count']==504
    assert event['complete'] and event['prompt_bytes']==len(prompts[0].encode())
    assert store.get_run(result['run_id'])['config']['reflection']['scope']=='all_training'


def test_oversized_full_input_is_archived_and_fails_before_model_call(tmp_path):
    class Proposer:
        config={'max_prompt_bytes':100}
        def __call__(self,prompt):
            pytest.fail('Oversized reflection must never call the model')
    def evaluator(spec, ep, jev, capture_traces=True):return detailed_result(ep,0.)
    store=RunStore(tmp_path)
    with pytest.raises(ReflectionContextError,match='no text was truncated'):
        run_evolution([{'id':'train'}],[{'id':'validation'}],jev=JevClient(mock=True),proposer=Proposer(),
                      evaluator=evaluator,task_id='test',seed_pipeline=candidate(),store=store,max_metric_calls=3)
    run=store.list_runs()[0]
    assert run['status']=='failed'
    event=store.list_events(run['run_id'],'reflection_input')[0]['payload']
    assert not event['context_fits'] and not event['dispatched']
    payload=json.loads((store.run_dir(run['run_id'])/event['path']).read_text())
    assert payload['training_feedback']['pipeline'][0]['result']==detailed_result({'id':'train'},0.)
    assert not store.list_events(run['run_id'],'reflection_dispatch')


def test_full_feedback_retains_custom_trajectory_fields_and_is_independent():
    result={'traces':[{'nested':list(range(100))}]}
    trajectory={'episode_id':'a','custom':'retain me','result':result}
    batch=SimpleNamespace(trajectories=[trajectory],outputs=[result],scores=[.2])
    before=copy.deepcopy(trajectory)
    feedback=full_feedback(batch)
    assert feedback['pipeline'][0]['custom']=='retain me'
    feedback['pipeline'][0]['result']['traces'].clear()
    assert trajectory==before
    with pytest.raises(ValueError,match='complete trajectory'):
        full_feedback(SimpleNamespace(trajectories=None,outputs=[result],scores=[.2]))


def test_invalid_task_score_preserves_completed_traces_for_reflection(tmp_path):
    seen=[]
    def evaluator(spec,ep,jev,capture_traces=True):
        return {'score':float('nan') if spec['output']=='0' else 1.,'traces':[{'trace':[{'id':'completed','output':'keep this evidence'}]}]}
    def proposer(prompt):
        record=json.loads(prompt)['training_feedback']['pipeline'][0]['result']
        assert record['traces'][0]['trace'][0]['output']=='keep this evidence'
        assert 'invalid score' in record['error']
        seen.append(record)
        return json.dumps(candidate('1'))
    result=run_evolution([{'id':'train'}],[{'id':'validation'}],jev=JevClient(mock=True),proposer=proposer,
        evaluator=evaluator,task_id='test',seed_pipeline=candidate(),store=RunStore(tmp_path),max_metric_calls=3)
    assert seen and result['best_score']==1.
