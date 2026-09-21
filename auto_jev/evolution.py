"""Native GEPA search over complete typed pipelines, with replaceable task evaluation."""
import copy
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path
import httpx
import gepa
from gepa.core.adapter import EvaluationBatch
from gepa.strategies.candidate_selector import ParetoCandidateSelector
from gepa.strategies.batch_sampler import EpochShuffledBatchSampler
from gepa.gepa_utils import remove_dominated_programs
from .spec import validate_spec,spec_hash,seed_spec,pipeline_schema,_json
from .crypto import evaluate_episode
from .providers import JevProviderError
from .vercel import VercelError
from .frozen import evaluate_frozen,source_hash as runtime_source_hash,digest as artifact_digest,verify_task_contract
from .observations import OBSERVATION_CONFIG
from .reflection import full_feedback, archive_prompt, prompt_limit, atomic_text
from .runtime import PipelineExecutionError
from .storage import RunStore,episode_summary,episode_data_kind,now_iso,atomic_write_json,read_json

FAILURE_SCORE=-1e9


def _digest(data):return hashlib.sha256(json.dumps(data,sort_keys=True,allow_nan=False).encode()).hexdigest()


def _archive_evaluation(store,run_id,candidate,episode_id,split,result,*,capture_traces,round_number):
    """Publish a complete result once; latest UI traces are not audit history.

    Unlike the UI JSON writer this must not coerce values or trim deep fields.
    A fsynced temporary file is linked into place without replacing an existing
    object. Existing bytes must match, including cache and timing metadata.
    """
    if split not in ('train','validation'):raise ValueError('Evaluation history only accepts train/validation')
    raw=json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode('utf-8')
    sha=hashlib.sha256(raw).hexdigest()
    relative=f'evaluation_history/{sha}.json'
    path=store.run_dir(run_id)/relative
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.parent.resolve()!=path.parent:raise ValueError('Evaluation history directory cannot be a symlink')
    fd,temporary=tempfile.mkstemp(prefix='.evaluation-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes()!=raw:
                raise ValueError('Existing evaluation history does not match its content hash')
        directory_fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    manifest={'schema':'auto_jev.evaluation-result.v1','candidate':candidate,'episode_id':episode_id,
              'split':split,'sha256':sha,'path':relative,'bytes':len(raw),
              'capture_traces':capture_traces,'round':round_number}
    # A failed event write propagates: an orphaned object is not claimed as
    # published evidence and reflection must not proceed without its binding.
    store.append_event(run_id,'evaluation_archived',manifest)
    return manifest


def _round_callback(method):
    # GEPA logs and swallows callback exceptions. Latch them so evaluation and
    # stopping cannot silently continue after a broken journal/progress write.
    def guarded(self,event):
        try:return method(self,event)
        except Exception as exc:
            self.error=exc
            if self.active:self.active['error']=str(exc)
            raise
    return guarded


class _EvolutionRounds:
    """Count committed reflection rounds, independently of GEPA metric calls.

    GEPA persists adapter state before each iteration and at normal shutdown.
    The small response journal additionally permits replay of a returned proposal
    if a process dies before its GEPA checkpoint is committed. This is not an
    exactly-once guarantee for a provider response lost before journaling.
    """
    def __init__(self,store,run_id,target,train_ids,*,resuming=False,skip_perfect_score=False):
        self.store,self.run_id,self.target=store,run_id,target
        self.train_ids=train_ids
        self.path=store.run_dir(run_id)/'rounds.json'
        self.journal=read_json(self.path,{}) if resuming else {}
        self.completed=self.accepted=0
        self.skip_perfect_score=skip_perfect_score
        self.skipped_batches=0
        self.perfect_coverage={}
        self.converged_candidate=None
        self.active=None
        self.error=None
        self.restored=not resuming
        self.selector=self.sampler=None
        self.jev=self.proposer=None

    def progress(self,**fields):
        current=self.store.get_run(self.run_id).get('progress',{})
        current.update(fields)
        current.update(rounds_target=self.target,rounds_completed=self.completed,
                       rounds_accepted=self.accepted,
                       skipped_batches=self.skipped_batches,
                       converged=self.converged_candidate is not None,
                       converged_candidate=self.converged_candidate,
                       perfect_training_coverage={k:len(v) for k,v in self.perfect_coverage.items()},
                       round_active=self.active['number'] if self.active else None)
        current.update(jev=dict(getattr(self.jev,'stats',{})),proposer=dict(getattr(self.proposer,'stats',{})))
        self.store.update_run(self.run_id,progress=current)

    def event(self,kind,payload):
        self.store.append_event(self.run_id,kind,payload)

    def persist(self):
        self.journal.update(completed=self.completed,accepted=self.accepted,target=self.target,
                            skipped_batches=self.skipped_batches,perfect_coverage=self.perfect_coverage,
                            converged_candidate=self.converged_candidate)
        atomic_write_json(self.path,self.journal)

    def get_state(self):
        return {'rounds_version':1,'completed':self.completed,'accepted':self.accepted,
                'skip_perfect_score':self.skip_perfect_score,'skipped_batches':self.skipped_batches,
                'perfect_coverage':copy.deepcopy(self.perfect_coverage),
                'converged_candidate':self.converged_candidate,
                'selector_rng':self.selector.native.rng.getstate(),
                'sampler_rng':self.sampler.rng.getstate(),
                'sampler':copy.deepcopy({k:v for k,v in self.sampler.__dict__.items() if k!='rng'})}

    def restore(self,state):
        if not state:
            if not self.restored:raise ValueError('Checkpoint predates resumable evolution rounds')
            return
        if state.get('rounds_version')!=1:raise ValueError('Unsupported evolution rounds checkpoint')
        if state.get('skip_perfect_score',False)!=self.skip_perfect_score:raise ValueError('Checkpoint skip_perfect_score differs')
        self.completed,self.accepted=state['completed'],state['accepted']
        self.skipped_batches=state.get('skipped_batches',0)
        self.perfect_coverage=copy.deepcopy(state.get('perfect_coverage',{}))
        self.converged_candidate=state.get('converged_candidate')
        self.selector.native.rng.setstate(state['selector_rng'])
        self.sampler.rng.setstate(state['sampler_rng'])
        self.sampler.__dict__.update(copy.deepcopy(state['sampler']))
        self.restored=True
        self.event('round_checkpoint_restored',{'completed':self.completed,'accepted':self.accepted,
                   'skipped_batches':self.skipped_batches,'converged_candidate':self.converged_candidate,
                   'pending_round':(self.journal.get('pending') or {}).get('number')})
        self.progress(status='restored')

    @_round_callback
    def on_iteration_start(self,event):
        # A caller may invoke evolution while handling an unrelated exception.
        # Only a newly unwinding exception at iteration end belongs to this round.
        self.entry_exception=sys.exc_info()[1]
        self.active={'number':self.completed+1,'iteration':event['iteration'],
                     'episode_ids':[],'returned':False,'replayed':False,'error':None,'skipped':False}
        self.event('round_started',copy.deepcopy(self.active))
        self.progress(status='evaluating_parent')

    @_round_callback
    def on_candidate_selected(self,event):
        self.active.update(parent=spec_hash(json.loads(event['candidate']['pipeline'])),
                           candidate_idx=event['candidate_idx'])

    @_round_callback
    def on_minibatch_sampled(self,event):
        ids=[self.train_ids[index] for index in event['minibatch_ids']]
        self.active['episode_ids']=ids
        self.event('round_batch',{'round':self.active['number'],'iteration':event['iteration'],'episode_ids':ids})
        self.progress(batch_episode_ids=ids)

    def parent_evaluated(self,parent,episode_ids,scores):
        if self.active.get('parent')!=parent or self.active['episode_ids']!=episode_ids:
            raise ValueError('Parent evaluation differs from the sampled candidate/batch')
        self.active['scores']=list(scores)

    @_round_callback
    def on_evaluation_skipped(self,event):
        # Only this native skip is a valid proposal-free iteration. In particular,
        # missing trajectories and unrecognised skip reasons remain fatal.
        if event['reason']!='all_scores_perfect':return
        active=self.active
        scores=event.get('scores') or []
        if (not self.skip_perfect_score or not active or active['returned'] or active['skipped']
                or event['iteration']!=active['iteration']
                or event['candidate_idx']!=active.get('candidate_idx')
                or not active['episode_ids'] or len(scores)!=len(active['episode_ids'])
                or scores!=active.get('scores')
                or any(isinstance(s,bool) or not isinstance(s,(int,float)) or not math.isfinite(s) or s<1.0 for s in scores)):
            raise RuntimeError('Invalid GEPA all_scores_perfect skip')
        if (self.journal.get('pending') or {}).get('number')==self.completed+1:
            raise RuntimeError('A journaled reflection proposal cannot be discarded by a perfect-score skip')
        active['skipped']=True

    def update_perfect_coverage(self):
        if not self.skip_perfect_score:return
        # Coverage is per immutable parent, never the union of different policies.
        # These are observed training results, not a claim about unseen episodes.
        parent=self.active['parent']
        covered=set(self.perfect_coverage.get(parent,[]))
        for episode_id,score in zip(self.active['episode_ids'],self.active['scores'],strict=True):
            if score>=1.0:covered.add(episode_id)
            else:covered.discard(episode_id)
        self.perfect_coverage[parent]=sorted(covered)
        if covered==set(self.train_ids):
            self.converged_candidate=parent
            self.event('training_perfect',{'candidate':parent,'episode_ids':sorted(covered),
                       'training_size':len(self.train_ids),'perfect_score':1.0,
                       'rounds_completed':self.completed,'rounds_target':self.target,
                       'criterion':'same_candidate_observed_perfect_on_all_training_episodes'})

    @_round_callback
    def on_error(self,event):
        self.error=event['exception']
        if self.active:self.active['error']=str(event['exception'])

    @_round_callback
    def on_iteration_end(self,event):
        if not self.active:return
        unwinding=sys.exc_info()[1]
        if unwinding is not None and unwinding is not self.entry_exception:
            self.error=unwinding
            self.active['error']=f'{type(unwinding).__name__}: {unwinding}'
        if not self.active['returned'] and not self.active['skipped'] and self.error is None:
            self.error=RuntimeError('GEPA iteration ended without a reflection proposal response')
            self.active['error']=str(self.error)
        if self.active['error'] is None and self.error is None:
            if self.active['skipped']:
                if event['proposal_accepted']:raise RuntimeError('A skipped iteration cannot accept a proposal')
                self.skipped_batches+=1
                self.event('round_skipped',{**self.active,'reason':'all_scores_perfect',
                           'completed':self.completed,'skipped_batches':self.skipped_batches,
                           'metric_calls':event['state'].total_num_evals})
            else:
                self.completed+=1
                self.accepted+=int(event['proposal_accepted'])
                self.event('round_completed',{**self.active,'accepted':bool(event['proposal_accepted']),
                           'completed':self.completed,'accepted_total':self.accepted,
                           'metric_calls':event['state'].total_num_evals})
            self.update_perfect_coverage()
        else:
            self.event('round_failed',{**self.active,'completed':self.completed})
        self.persist()
        skipped=self.active['skipped']
        self.active=None
        self.progress(status='failed' if self.error is not None else 'training_perfect' if self.converged_candidate else 'batch_skipped' if skipped else 'round_complete')

    def replay(self,parent,episode_ids):
        pending=self.journal.get('pending') or {}
        if pending.get('number')!=self.completed+1:return None
        if pending.get('parent')!=parent or pending.get('episode_ids')!=episode_ids:
            raise ValueError('Pending proposal differs from restored parent/batch; refusing unsafe replay')
        self.active.update(returned=True,replayed=True,parent=parent)
        self.event('reflection_replayed',{'round':self.active['number'],'parent':parent,
                   'episode_ids':episode_ids,'response_path':pending.get('response_path')})
        return copy.deepcopy(pending['candidate'])

    def returned(self,parent,episode_ids,candidate,response_path,valid):
        self.journal['pending']={'number':self.active['number'],'parent':parent,'episode_ids':episode_ids,
                                 'candidate':copy.deepcopy(candidate),'response_path':response_path,'valid':valid}
        self.persist()
        self.active.update(returned=True,parent=parent,valid=valid)
        self.progress(status='evaluating_proposal')

    def stop(self,state):
        return self.error is not None or self.converged_candidate is not None or (self.target is not None and self.completed>=self.target)


def run_evolution(train,validation,*,jev,proposer,store=None,run_name='evolution',max_metric_calls=None,seed=0,costs=None,seed_pipeline=None,evaluator=None,task_id='crypto_spot',reflection_batch_size=None,evolution_rounds=None,resume_run_id=None,task_context=None,task_contract=None,skip_perfect_score=False):
    if not train or not validation:raise ValueError('train and validation must be nonempty')
    ids=[ep['id'] for ep in train+validation]
    if len(set(ids))!=len(ids):raise ValueError('episode IDs must be unique across train and validation')
    batch_size=len(train) if reflection_batch_size is None else reflection_batch_size
    if isinstance(batch_size,bool) or not isinstance(batch_size,int) or not 1<=batch_size<=len(train):raise ValueError('reflection_batch_size must be between 1 and the training set size')
    if evolution_rounds is not None and (isinstance(evolution_rounds,bool) or not isinstance(evolution_rounds,int) or evolution_rounds<1):raise ValueError('evolution_rounds must be a positive integer')
    if not isinstance(skip_perfect_score,bool):raise ValueError('skip_perfect_score must be a boolean')
    if max_metric_calls is None and evolution_rounds is None:max_metric_calls=24
    if max_metric_calls is not None and (isinstance(max_metric_calls,bool) or not isinstance(max_metric_calls,int) or max_metric_calls<len(validation)+2*batch_size):raise ValueError('budget must allow initial validation and a full parent/child training comparison')
    max_prompt_bytes=prompt_limit(proposer)
    reflection_encoding=getattr(proposer,'config',{}).get('reflection_encoding','plain')
    if reflection_encoding not in ('plain','lossless_dag'):raise ValueError('reflection_encoding must be plain or lossless_dag')
    # Shared per-asset time ranges may overlap within a split, never between splits.
    if task_id=='crypto_spot':
        train_end=max(ep['bars'][-1]['timestamp']+ep['interval_seconds'] for ep in train)
        validation_start=min(ep['bars'][0]['timestamp'] for ep in validation)
        if train_end>validation_start:raise ValueError('training must end before validation begins')
    task=evaluator or evaluate_episode
    task_context=copy.deepcopy(task_context) if task_context is not None else {}
    if not isinstance(task_context,dict):raise ValueError('task_context must be a JSON object')
    _json(task_context)
    if 'objective' in task_context and (not isinstance(task_context['objective'],str) or not task_context['objective'].strip()):raise ValueError('task_context objective must be a nonempty string')
    task_contract=verify_task_contract(task_contract,evaluator=task) if task_contract is not None else None
    costs=dict(costs or ({'initial_cash':10000.,'fee_bps':10.,'slippage_bps':5.} if task_id=='crypto_spot' else {}))
    pipeline=validate_spec(seed_pipeline or seed_spec());store=store or RunStore()
    kinds={episode_data_kind(e) for e in train+validation}
    config={'source_hash':runtime_source_hash(),'observation_config':dict(OBSERVATION_CONFIG),'task_id':task_id,'costs':costs,'seed':seed,'max_metric_calls':max_metric_calls,
            'evolution_rounds':evolution_rounds,'seed_pipeline_hash':spec_hash(pipeline),
            'skip_perfect_score':skip_perfect_score,'perfect_score':1.0 if skip_perfect_score else None,
            'reflection':{'mode':'full','batch_size':batch_size,'scope':'all_training' if batch_size==len(train) else 'explicit_batch','max_prompt_bytes':max_prompt_bytes,'encoding':reflection_encoding},
            'data_kind':next(iter(kinds)) if len(kinds)==1 else 'mixed','jev_mock':getattr(jev,'mock',False),
            'proposer_label':getattr(proposer,'metadata',{}).get('kind','demo' if proposer is demo_proposer else 'custom'),
            'episodes':[episode_summary(e,s) for s,eps in [('train',train),('validation',validation)] for e in eps],
            'data_hashes':{e['id']:_digest(e) for e in train+validation},'objective':task_context.get('objective','highest net profit at identical initial cash' if task_id=='crypto_spot' else 'maximize task score'),
            'task_context':task_context,'task_contract':task_contract}
    if task_id=='crypto_spot' and len({len(ep['bars']) for ep in train})==1:
        config['reflection']['batch_bars']=len(train[0]['bars'])
    has_checkpoint=False
    if resume_run_id is not None:
        run_id=resume_run_id;previous=store.get_run(run_id);old=previous['config']
        has_checkpoint=(store.run_dir(run_id)/'gepa'/'gepa_state.bin').exists()
        if not has_checkpoint and (store.list_events(run_id,'round_started') or store.list_events(run_id,'round_completed')):raise ValueError('Run has evolution rounds but no GEPA checkpoint to resume')
        for key in ('source_hash','observation_config','task_id','costs','seed','data_hashes','seed_pipeline_hash'):
            if old.get(key)!=config[key]:raise ValueError(f'Resume contract mismatch: {key}')
        if (old.get('task_context') or {})!=task_context:raise ValueError('Resume contract mismatch: task_context')
        if old.get('task_contract')!=task_contract:raise ValueError('Resume contract mismatch: task_contract')
        if [(e['id'],e['split']) for e in old['episodes']]!=[(e['id'],e['split']) for e in config['episodes']]:raise ValueError('Resume episode order/split differs')
        if old.get('reflection',{}).get('batch_size')!=batch_size:raise ValueError('Resume reflection batch size differs')
        if old.get('reflection',{}).get('encoding','plain')!=reflection_encoding:raise ValueError('Resume reflection encoding differs')
        if old.get('skip_perfect_score',False)!=skip_perfect_score:raise ValueError('Resume skip_perfect_score differs')
        if previous.get('jev_metadata',{}).get('cache_namespace')!=getattr(jev,'metadata',{}).get('cache_namespace'):raise ValueError('Resume Jev cache namespace differs')
        for name,provider in [('jev',jev),('proposer',proposer)]:
            stats=getattr(provider,'stats',None)
            if isinstance(stats,dict):stats.update(previous.get('progress',{}).get(name,{}))
        if isinstance(getattr(jev,'metadata',None),dict):jev.metadata.update(previous.get('jev_metadata',{}))
        store.append_event(run_id,'run_resumed',{'target':evolution_rounds,'max_metric_calls':max_metric_calls,
                           'risk':'An unjournaled provider return may require another call; committed checkpoints and recorded proposals are reused.'})
    else:run_id=store.create_run(run_name,config)
    store.update_run(run_id,status='running',config=config,error=None,jev_metadata=getattr(jev,'metadata',{}),proposer_metadata=getattr(proposer,'metadata',{}))
    store.save_candidate(run_id,pipeline,spec_hash(pipeline),origin='seed')
    reflection_abort=None
    rounds=_EvolutionRounds(store,run_id,evolution_rounds,[e['id'] for e in train],resuming=has_checkpoint,skip_perfect_score=skip_perfect_score)
    rounds.jev,rounds.proposer=jev,proposer
    rounds.progress(status='starting')
    resume_seed={r['episode_id']:r for r in store.list_evaluations(run_id,candidate=spec_hash(pipeline),split='validation')} if resume_run_id else {}

    class Adapter:
        propose_new_texts=None
        def get_adapter_state(self):return rounds.get_state()
        def set_adapter_state(self,state):rounds.restore(state)
        def batch_evaluate(self,items):
            # GEPA 0.1.4's fallback requests full traces for *every* batch,
            # including accepted-candidate validation. Only the parent training
            # batch is consumed by reflection; child/validation outputs stay light.
            return [self.evaluate(batch,candidate,capture_traces=bool(
                        rounds.active and not rounds.active['returned'] and
                        all(item['split']=='train' for item in batch)))
                    for candidate,batch in items]
        def evaluate(self,batch,candidate,capture_traces=False):
            if reflection_abort is not None:raise reflection_abort
            if rounds.error is not None:raise rounds.error
            outputs=[];scores=[];trajectories=[]
            spec=validate_spec(json.loads(candidate['pipeline']));ident=spec_hash(spec)
            if not rounds.restored:
                if ident!=spec_hash(pipeline) or capture_traces or any(item['split']!='validation' for item in batch):raise ValueError('Unexpected evaluation before checkpoint restoration')
                rows=[resume_seed[item['episode']['id']] for item in batch]
                rounds.event('resume_seed_reused',{'episode_ids':[r['episode_id'] for r in rows]})
                return EvaluationBatch(outputs=rows,scores=[r['score'] for r in rows])
            store.save_candidate(run_id,spec,ident)
            for item in batch:
                ep=item['episode'];split=item['split'];start=time.perf_counter()
                saved=resume_seed.get(ep['id'])
                if resume_run_id and not has_checkpoint and ident==spec_hash(pipeline) and split=='validation' and saved and saved['status']=='ok':
                    outputs.append(saved);scores.append(saved['score'])
                    rounds.event('resume_seed_reused',{'episode_ids':[ep['id']],'initial_validation':True})
                    continue
                rounds.event('episode_started',{'candidate':ident,'episode_id':ep['id'],'split':split,'round':rounds.active['number'] if rounds.active else None,'capture_traces':capture_traces})
                rounds.progress(current_candidate=ident,current_episode=ep['id'],current_split=split,status='evaluating')
                fatal_error=None;result=None
                try:
                    result=task(spec,ep,jev,capture_traces=True,**costs)
                    score=result['score']
                    if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score):raise ValueError('Task returned an invalid score')
                    status='ok'
                except KeyboardInterrupt as exc:
                    rounds.error=exc
                    raise
                except PipelineExecutionError as exc:
                    cause=exc.cause
                    fatal_error=next((error for error in getattr(exc,'causes',(cause,)) if isinstance(error,(JevProviderError,VercelError,httpx.HTTPError)) or getattr(error,'task_infrastructure_error',False)),None)
                    score=FAILURE_SCORE;status='error';result=copy.deepcopy(exc.partial_result)
                    result.update(episode_id=ep['id'],score=score,error=str(exc))
                    if 'traces' not in result:result={'episode_id':ep['id'],'score':score,'error':str(exc),'traces':[result]}
                except (JevProviderError,VercelError,httpx.HTTPError) as exc:
                    fatal_error=exc;score=FAILURE_SCORE;status='error'
                    result={'episode_id':ep['id'],'score':score,'error':str(exc),'traces':[]}
                except (ValueError,KeyError,IndexError,TypeError,ArithmeticError) as exc:
                    score=FAILURE_SCORE;status='error'
                    result=copy.deepcopy(result) if isinstance(result,dict) else {}
                    result.update(episode_id=ep['id'],score=score,error=str(exc))
                    result.setdefault('traces',[])
                record={'candidate':ident,'episode_id':ep['id'],'split':split,'status':status,'score':score,
                        'net_profit':result.get('net_profit'),'return_pct':result.get('return_pct'),'max_drawdown_pct':result.get('max_drawdown_pct'),
                        'n_trades':len(result.get('trades',[])),'elapsed_ms':(time.perf_counter()-start)*1000,'error':result.get('error')}
                history=_archive_evaluation(store,run_id,ident,ep['id'],split,result,
                    capture_traces=capture_traces,round_number=rounds.active['number'] if rounds.active else None)
                record['evaluation_history']=history
                store.append_evaluation(run_id,record);store.save_trace(run_id,split,ident,ep['id'],result)
                rounds.progress(last_episode=ep['id'],last_split=split,status='episode_complete')
                if fatal_error is not None:raise fatal_error
                # Complete results are always archived above; persistent GEPA
                # validation state holds only compact scalar metrics/errors.
                outputs.append(result if capture_traces else {k:v for k,v in result.items() if k in ('episode_id','error','status') or v is None or isinstance(v,(int,float,bool))})
                scores.append(score)
                if capture_traces:trajectories.append({'episode_id':ep['id'],'episode':copy.deepcopy(ep),
                                                       'result':copy.deepcopy(result),'evaluation_history':history})
            if capture_traces:rounds.parent_evaluated(ident,[item['episode']['id'] for item in batch],scores)
            return EvaluationBatch(outputs=outputs,scores=scores,trajectories=trajectories if capture_traces else None)
        def make_reflective_dataset(self,candidate,eval_batch,components_to_update):
            return full_feedback(eval_batch)

    class Selector:
        def __init__(self):self.native=ParetoCandidateSelector(random.Random(seed))
        def select_candidate_idx(self,state):
            mapping=state.get_pareto_front_mapping()
            frontier=remove_dominated_programs(mapping,state.per_program_tracked_scores)
            counts={}
            for winners in frontier.values():
                for idx in winners:counts[idx]=counts.get(idx,0)+1
            total=sum(counts.values());chosen=self.native.select_candidate_idx(state)
            hashes={i:spec_hash(json.loads(c['pipeline'])) for i,c in enumerate(state.program_candidates)}
            store.append_event(run_id,'parent_selected',{'candidate':hashes[chosen],'gepa_idx':chosen,
                'probabilities':{str(i):n/total for i,n in counts.items()},'candidate_hashes':hashes,
                'frontier':{str(k):sorted(v) for k,v in frontier.items()},'raw_frontier':{str(k):sorted(v) for k,v in mapping.items()}})
            return chosen

    def propose_impl(candidate,reflective_dataset,components_to_update):
        nonlocal reflection_abort
        if reflection_abort is not None:return candidate.copy()
        parent=json.loads(candidate['pipeline']);parent_hash=spec_hash(parent)
        episode_ids=[record['episode_id'] for record in reflective_dataset['pipeline']]
        if rounds.error is not None:
            reflection_abort=rounds.error
            return candidate.copy()
        version_instruction=('Use version 3. Evolve Python source functions run(obs, nodes, memory), expression nodes, Jev nodes, and the dependency graph together. Python nodes must declare explicit depends_on for every node output they read. Jev questions_expression may construct the complete runtime question map from preceding code outputs; keep provider question/criteria constraints and only select actions offered by the task. Python executes in isolation and cannot change the evaluator or access its private state. '
                             if pipeline['version']>=3 else 'Use version 2 to add/delete/rewire feature nodes, parallel Jev branches, multiple decision stages, question instructions, criteria, output and memory rules. ')
        # Legacy v1 seeds could already evolve into v2 graphs. Keep that schema
        # while pinning v3 seeds to their new code + dynamic-question contract.
        schema_version=max(2,pipeline['version'])
        schema=pipeline_schema(version=schema_version) if 'version' in inspect.signature(pipeline_schema).parameters else pipeline_schema()
        payload={'instruction':'Evolve the complete Jev dependency graph on the stated task. Return ONLY a complete PipelineSpec JSON object, with no explanation. '+version_instruction+'You are not restricted to one Jev call, the seed topology or small prompt edits. Independent nodes run in parallel; dependent nodes wait for their inputs. Inspect ALL supplied complete training trajectories including intermediate successes, failures, finalization, decisions and outcomes. Training episode outcomes are hindsight feedback, never runtime observations. Do not change the Jev model, task contracts or evaluator.',
                           'task':{'id':task_id,'objective':config['objective'],'fixed_costs':costs,'context':task_context},'schema':schema,'parent':parent,'training_feedback':reflective_dataset}
        try:
            replay=rounds.replay(parent_hash,episode_ids)
            if replay is not None:return replay
            prompt,manifest=archive_prompt(store,run_id,parent_hash,payload,max_prompt_bytes,encoding=reflection_encoding)
            rounds.progress(status='reflecting')
            store.append_event(run_id,'reflection_dispatch',{'sha256':manifest['sha256'],'path':manifest['path'],'round':rounds.active['number'],'episode_ids':episode_ids})
            text=proposer(prompt).strip()
        except Exception as exc:
            # GEPA retries custom-proposer exceptions internally. Latch systemic
            # failures so it cannot resend a paid call or hide an oversized input.
            reflection_abort=exc
            rounds.error=exc
            store.append_event(run_id,'reflection_error',{'parent':parent_hash,'error':f'{type(exc).__name__}: {exc}'})
            return candidate.copy()
        response_path=manifest['path'].replace('.prompt.json','.response.txt')
        atomic_text(store.run_dir(run_id)/response_path,text)
        store.append_event(run_id,'reflection_complete',{'sha256':manifest['sha256'],'response_bytes':len(text.encode('utf-8')),
                           'proposer':copy.deepcopy(getattr(proposer,'metadata',{})),
                           'stats':copy.deepcopy(getattr(proposer,'stats',{}))})
        if text.startswith('```'):text='\n'.join(text.splitlines()[1:-1])
        try:
            new=validate_spec(json.loads(text))
            if pipeline['version']>=2 and new['version']!=pipeline['version']:raise ValueError(f"Version {pipeline['version']} graph evolution must remain version {pipeline['version']}")
            if new['jev_model']!=pipeline['jev_model']:raise ValueError('Cannot change the fixed Jev model')
        except (ValueError,TypeError) as exc:
            store.append_event(run_id,'proposal_rejected',{'parent':parent_hash,'reason':str(exc)[:500]})
            rounds.returned(parent_hash,episode_ids,candidate,response_path,False)
            return candidate.copy()
        ident=spec_hash(new);store.save_candidate(run_id,new,ident,parents=[parent_hash],origin='proposal')
        store.append_event(run_id,'proposal',{'parent':parent_hash,'candidate':ident,'proposer':getattr(proposer,'metadata',{}),'stats':getattr(proposer,'stats',{})})
        proposed={'pipeline':json.dumps(new,sort_keys=True)}
        rounds.returned(parent_hash,episode_ids,proposed,response_path,True)
        return proposed

    def propose(candidate,reflective_dataset,components_to_update):
        nonlocal reflection_abort
        try:return propose_impl(candidate,reflective_dataset,components_to_update)
        except Exception as exc:
            # Includes response/journal persistence failures after the model
            # returns: GEPA must not retry an already charged proposer call.
            reflection_abort=rounds.error=exc
            return candidate.copy()

    rounds.selector=Selector()
    rounds.sampler=EpochShuffledBatchSampler(batch_size,random.Random(seed))
    try:
        result=gepa.optimize(seed_candidate={'pipeline':json.dumps(pipeline,sort_keys=True)},
            trainset=[{'split':'train','episode':e} for e in train],valset=[{'split':'validation','episode':e} for e in validation],
            adapter=Adapter(),custom_candidate_proposer=propose,reflection_lm=None,candidate_selection_strategy=rounds.selector,
            frontier_type='instance',module_selector='all',skip_perfect_score=skip_perfect_score,perfect_score=1.0,acceptance_criterion='strict_improvement',
            val_evaluation_policy='full_eval',use_merge=False,cache_evaluation=False,max_metric_calls=max_metric_calls,
            batch_sampler=rounds.sampler,stop_callbacks=rounds.stop,callbacks=[rounds],
            seed=seed,run_dir=str(store.run_dir(run_id)/'gepa'),display_progress_bar=False,raise_on_exception=True)
        if reflection_abort is not None:raise reflection_abort
        if rounds.error is not None:raise rounds.error
        hashes=[spec_hash(json.loads(c['pipeline'])) for c in result.candidates]
        for i,candidate in enumerate(result.candidates):
            store.save_candidate(run_id,json.loads(candidate['pipeline']),hashes[i],parents=[hashes[j] for j in result.parents[i] if j is not None],origin='accepted',gepa_idx=i,extra={'val_aggregate':result.val_aggregate_scores[i]})
        valid=[i for i,scores in enumerate(result.val_subscores) if len(scores)==len(validation) and all(s>FAILURE_SCORE for s in scores.values())]
        if not valid:raise ValueError('No candidate passed all validation episodes')
        best=max(valid,key=lambda i:result.val_aggregate_scores[i])
        summary={'run_id':run_id,'best_hash':hashes[best],'best_score':result.val_aggregate_scores[best],'best_idx':best,
                 'candidates':hashes,'parents':result.parents,'validation_scores':result.val_subscores,'total_metric_calls':result.total_metric_calls,
                 'rounds_target':evolution_rounds,'rounds_completed':rounds.completed,'rounds_accepted':rounds.accepted,
                 'skipped_batches':rounds.skipped_batches,'converged':rounds.converged_candidate is not None,
                 'converged_candidate':rounds.converged_candidate}
        goal_reached=evolution_rounds is None or rounds.completed>=evolution_rounds
        summary.update(goal_reached=goal_reached,stop_reason='training_perfect' if summary['converged'] else 'round_target' if evolution_rounds is not None and goal_reached else 'metric_budget' if max_metric_calls is not None and result.total_metric_calls>=max_metric_calls else 'external_stop')
        status='completed' if goal_reached or summary['converged'] else 'paused'
        rounds.progress(status=status,current_candidate=None,current_episode=None,current_split=None)
        store.update_run(run_id,status=status,result=summary,jev_metadata=getattr(jev,'metadata',{}),proposer_metadata=getattr(proposer,'metadata',{}))
        return summary
    except BaseException as exc:
        # Cleanup only: never turn SystemExit/interrupts into scores or swallow
        # their control flow. The iteration callback also refuses to count them.
        rounds.progress(status='failed')
        store.update_run(run_id,status='failed',error=f'{type(exc).__name__}: {str(exc)[:500]}',jev_metadata=getattr(jev,'metadata',{}),proposer_metadata=getattr(proposer,'metadata',{}))
        raise


def freeze_run(store,run_id,candidate_hash=None):
    run=store.get_run(run_id)
    if run['status']!='completed':raise ValueError('Only completed runs can be frozen')
    if run['config'].get('task_contract') is not None:verify_task_contract(run['config']['task_contract'])
    ident=candidate_hash or run['result']['best_hash'];candidate=store.get_candidate(run_id,ident)
    rows=store.list_evaluations(run_id,candidate=ident,split='validation')
    latest={r['episode_id']:r for r in rows};expected={e['id'] for e in run['config']['episodes'] if e['split']=='validation'}
    if set(latest)!=expected or any(r['status']!='ok' for r in latest.values()):raise ValueError('Candidate did not pass full validation')
    source_hash=runtime_source_hash()
    if run['config'].get('source_hash') != source_hash:
        raise ValueError('Runtime source changed since evaluation; evaluate a new run before freezing')
    config=run['config'];artifact={'version':1,'created_at':now_iso(),'run_id':run_id,'spec':candidate['spec'],'spec_hash':spec_hash(candidate['spec']),
        'task_id':config['task_id'],'costs':config['costs'],'observation_config':dict(OBSERVATION_CONFIG),'evaluation_source_hash':config.get('source_hash'),'data_hashes':config['data_hashes'],'source_hash':source_hash,
        'dependencies':{name:importlib.metadata.version(name) for name in ('auto-jev','gepa','httpx')},'jev_metadata':run.get('jev_metadata',{}),
        'model_version_note':'Gateway alias does not guarantee a fixed underlying model version. Stored responses support exact replay; fresh requests may change.'}
    if config.get('task_context'):artifact['task_context']=copy.deepcopy(config['task_context'])
    if config.get('task_contract') is not None:artifact['task_contract']=copy.deepcopy(config['task_contract'])
    artifact['artifact_hash']=artifact_digest(artifact)
    store.save_frozen(run_id,artifact);return artifact


def demo_proposer(prompt):
    parent=json.loads(prompt)['parent'];spec=copy.deepcopy(parent)
    proposals=['1.0','0.0',"1.0 if last(obs['closes']) > mean(obs['closes']) else 0.0",'.5']
    counter=getattr(demo_proposer,'count',0);demo_proposer.count=counter+1
    spec['name']=f'合成提案 {counter+1}';spec['nodes']=[];spec['output']=proposals[counter%len(proposals)]
    return json.dumps(spec)
