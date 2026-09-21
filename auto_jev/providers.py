"""Jev execution transports and interchangeable offline reflection providers."""
import copy
import hashlib
import json
import math
import re
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
import time
import uuid
from pathlib import Path
import httpx
from .storage import atomic_write_json
from .vercel import vercel_judge,VercelError,normalize_answers,_parse_unit_probability

class JevProviderError(RuntimeError):pass
class ProposerError(RuntimeError):pass

class JevClient:
    def __init__(self,api_key=None,model='typesafe-ai/jev',cache_dir=None,mock=False,transport='auto',timeout=60,cache_namespace=None,cache_only=False):
        self.mock=mock;self.model=model;self.timeout=timeout
        self.transport=transport
        if api_key and transport=='auto' and not mock:raise ValueError('Explicit api_key requires explicit transport')
        if transport=='auto':self.transport='vercel' if os.getenv('AI_GATEWAY_API_KEY') else 'typesafe'
        if self.transport not in ('vercel','typesafe'):raise ValueError('unknown Jev transport')
        self.key=api_key or os.getenv('AI_GATEWAY_API_KEY' if self.transport=='vercel' else 'TYPESAFE_API_KEY')
        self.cache_namespace=cache_namespace or uuid.uuid4().hex
        if not self.cache_namespace.replace('-','').isalnum():raise ValueError('Invalid cache namespace')
        self.cache_only=cache_only
        self.cache_dir=Path(cache_dir)/self.cache_namespace if cache_dir else None
        self.stats={'calls':0,'cache_hits':0,'input_tokens':0,'output_tokens':0,'elapsed_ms':0.,'cost_usd':0.,'unpriced_calls':0}
        self._base_metadata={'transport':'mock' if mock else self.transport,'requested_model':model,'model_version_pinned':False,'cache_namespace':self.cache_namespace,'cache_dir':str(self.cache_dir) if self.cache_dir else None}
        self.metadata=copy.deepcopy(self._base_metadata)
        self._state_lock=threading.Lock()
        self._cache_locks={}

    @contextmanager
    def _cache_request(self,key):
        # Only identical cached requests share a lock. Unrelated network calls
        # never hold the accounting lock and remain genuinely concurrent.
        if self.cache_dir is None:
            yield
            return
        with self._state_lock:
            entry=self._cache_locks.get(key)
            if entry is None:
                entry=[threading.Lock(),0]
                self._cache_locks[key]=entry
            entry[1]+=1
        try:
            with entry[0]:
                yield
        finally:
            with self._state_lock:
                entry[1]-=1
                if entry[1]==0:
                    del self._cache_locks[key]

    def judge(self,state,questions,model=None):
        start=time.perf_counter();model=model or self.model
        # Hash and send the same snapshot even if a caller subsequently mutates
        # its input while another node is executing.
        state,questions=copy.deepcopy(state),copy.deepcopy(questions)
        payload={'protocol':'vercel-v4-typesafe-v1-normalization-2','transport':'mock' if self.mock else self.transport,'model':model,'state':state,'questions':questions}
        key=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
        path=self.cache_dir/f'{key}.json' if self.cache_dir else None
        with self._cache_request(key):
            return self._judge_once(state,questions,model,key,path,start)

    def _judge_once(self,state,questions,model,key,path,start):
        if path and path.exists():
            try:
                result=json.loads(path.read_text())
                digest=result.pop('response_digest',None)
                actual=hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()
                if digest!=actual or result.get('request_hash')!=key:
                    raise JevProviderError('Cache response integrity check failed')
            except (ValueError,TypeError,AttributeError):
                raise JevProviderError('Cache response integrity check failed') from None
            result['cache_hit']=True
            result['elapsed_ms']=(time.perf_counter()-start)*1000
            with self._state_lock:
                self.metadata.update(copy.deepcopy(result.get('client_metadata',{})))
                self.stats['cache_hits']+=1
            return copy.deepcopy(result)
        if self.cache_only:raise JevProviderError('No recorded response for this input; cache-only replay cannot call the API')
        direct_model='jev-1.13.0' if model=='typesafe-ai/jev' else model
        if self.mock:
            closes=state.get('closes',[]) if isinstance(state,dict) else []
            probability=.7 if len(closes)>1 and closes[-1]>closes[0] else .3
            answers={}
            for ident,q in questions.items():
                if q['type']=='noul':answers[ident]={'type':'noul','noul':probability}
                else:
                    opts=list(q['criteria']) if q['type']=='choice' else [str(i) for i in range(len(q['criteria']))]
                    i=len(opts)-1 if probability>.5 else 0;probs={k:float(j==i) for j,k in enumerate(opts)}
                    answers[ident]={'type':q['type'],'probabilities':probs,q['type']:opts[i] if q['type']=='choice' else i,'confidence':1.}
            result={'answers':answers,'model':'mock-engineering-only','usage':{}}
        else:
            if not self.key:raise JevProviderError(f'Missing {"AI_GATEWAY_API_KEY" if self.transport=="vercel" else "TYPESAFE_API_KEY"}')
            try:
                if self.transport=='vercel':result=vercel_judge(self.key,state,questions,model=model,timeout=self.timeout)
                else:
                    for attempt in range(4):
                        response=httpx.post('https://api.typesafe.ai/v1/systemone',headers={'Authorization':'Bearer '+self.key},json={'model':direct_model,'state':state,'questions':questions},timeout=self.timeout)
                        if response.status_code not in (429,500,502,503,504,529) or attempt==3:break
                        time.sleep(.5*2**attempt)
                    if not response.is_success:raise JevProviderError(f'TypeSafe HTTP {response.status_code}')
                    result=response.json()
                    if set(result.get('answers',{}))!=set(questions):raise JevProviderError('Incomplete Jev response')
                    raw=result['answers']
                    converted={k:({'type':'boolean','probability':v.get('noul')} if questions[k]['type']=='noul' else v) for k,v in raw.items()}
                    result['answers']=normalize_answers(questions,converted,result.get('rounding'))
                    for qid,answer in result['answers'].items():
                        if 'confidence' in raw[qid]:answer['confidence']=_parse_unit_probability(raw[qid]['confidence'],qid)
            except (httpx.HTTPError,VercelError) as exc:raise JevProviderError(f'Jev request failed: {type(exc).__name__}') from None
        result['cache_hit']=False;result['request_hash']=key
        result['elapsed_ms']=(time.perf_counter()-start)*1000;result['request_elapsed_ms']=result['elapsed_ms']
        usage=result.get('usage') or {};result['usage_reported']=bool(result.get('usage'))
        input_tokens=usage.get('inputTokens',usage.get('input_tokens',0)) or 0
        output_tokens=usage.get('outputTokens',usage.get('output_tokens',0)) or 0
        meta=copy.deepcopy(result.get('gateway_metadata') or {})
        cost=meta.get('provider_metadata',{}).get('gateway',{}).get('cost') if meta.get('provider_metadata') else None
        cost=float(cost) if cost is not None else None
        metadata={**copy.deepcopy(self._base_metadata),'requested_model':model,'actual_model':result.get('model'),
                  'model_version_pinned':meta.get('model_version_pinned',self.transport=='typesafe' and not self.mock and bool(re.fullmatch(r'jev-\d+\.\d+\.\d+',direct_model))),
                  'gateway':meta}
        result['client_metadata']=copy.deepcopy(metadata)
        with self._state_lock:
            self.stats['calls']+=1;self.stats['elapsed_ms']+=result['elapsed_ms']
            self.stats['input_tokens']+=input_tokens;self.stats['output_tokens']+=output_tokens
            if cost is not None:self.stats['cost_usd']+=cost
            elif not self.mock:self.stats['unpriced_calls']+=1
            self.metadata.update(metadata)
        if path:
            cached=copy.deepcopy(result);cached['response_digest']=hashlib.sha256(json.dumps(cached,sort_keys=True,allow_nan=False).encode()).hexdigest();atomic_write_json(path,cached)
        return copy.deepcopy(result)

DEFAULT_MAX_PROMPT_BYTES=1_000_000

class Proposer:
    def __init__(self,config):
        self.config=dict(config)
        limit=self.config.setdefault('max_prompt_bytes',DEFAULT_MAX_PROMPT_BYTES)
        if isinstance(limit,bool) or not isinstance(limit,int) or limit<=0:
            raise ProposerError('max_prompt_bytes must be a positive integer')
        self.metadata={k:v for k,v in self.config.items() if k not in ('api_key','token','headers')}
        self.stats={'calls':0,'elapsed_ms':0.,'input_tokens':0,'output_tokens':0}
    def __call__(self,prompt):
        if not isinstance(prompt,str):raise ProposerError('Reflection prompt must be a string')
        try:prompt_bytes=len(prompt.encode('utf-8'))
        except UnicodeEncodeError:raise ProposerError('Reflection prompt must be valid UTF-8 text') from None
        limit=self.config['max_prompt_bytes']
        if prompt_bytes>limit:
            raise ProposerError(f'Reflection prompt is {prompt_bytes} UTF-8 bytes; max_prompt_bytes is {limit}')
        cfg=self.config;kind=cfg.get('kind','claude_cli');start=time.perf_counter()
        if kind=='claude_cli':
            executable=cfg.get('executable') or shutil.which('claude')
            if not executable:raise ProposerError('Claude CLI is not installed')
            model=cfg.get('model','claude-fable-5-1')
            args=[executable,'--print','--model',model,'--effort',cfg.get('effort','xhigh'),'--safe-mode','--tools','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--disable-slash-commands','--no-session-persistence','--output-format','json','--system-prompt','Return only the requested complete PipelineSpec JSON. Use only provided observations; no tools.']
            env={k:v for k,v in os.environ.items() if k not in ('OPENAI_API_KEY','AI_GATEWAY_API_KEY','TYPESAFE_API_KEY','ANTHROPIC_API_KEY')}
            with tempfile.TemporaryDirectory(prefix='auto-jev-reflect-') as directory:
                proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=directory,env=env,start_new_session=True)
                try:out,_=proc.communicate(prompt,timeout=cfg.get('timeout',600))
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.communicate()
                    raise ProposerError('Local reflection model timed out') from None
            try:response=json.loads(out)
            except ValueError:raise ProposerError('Claude returned invalid envelope JSON') from None
            if proc.returncode or response.get('is_error'):raise ProposerError('Claude reflection call failed')
            self.metadata['actual_models']=list(response.get('modelUsage',{}))
            if model not in self.metadata['actual_models']:raise ProposerError('Claude response did not confirm the requested model')
            text=response.get('result','')
            usage=response.get('modelUsage',{})
            self.stats['input_tokens']+=sum(v.get('inputTokens',0)+v.get('cacheReadInputTokens',0)+v.get('cacheCreationInputTokens',0) for v in usage.values())
            self.stats['output_tokens']+=sum(v.get('outputTokens',0) for v in usage.values())
            self.stats['reported_cost_usd']=self.stats.get('reported_cost_usd',0.)+sum(v.get('costUSD',0.) for v in usage.values())
        else:
            import openai
            key_name=cfg.get('api_key_env','ANTHROPIC_API_KEY' if kind=='anthropic' else 'OPENAI_DIRECT_API_KEY' if kind=='openai' else 'OPENAI_API_KEY');key=os.getenv(key_name)
            if not key:raise ProposerError(f'Missing {key_name}')
            try:
                if kind=='anthropic':
                    import anthropic
                    client=anthropic.Anthropic(api_key=key,timeout=cfg.get('timeout',180))
                    with client:
                        response=client.messages.create(model=cfg['model'],max_tokens=cfg.get('max_tokens',4096),messages=[{'role':'user','content':prompt}],**({'output_config':{'effort':cfg['effort']}} if 'effort' in cfg else {}))
                    text=''.join(x.text for x in response.content if x.type=='text')
                    self.metadata['actual_model']=response.model
                    self.stats['input_tokens']+=response.usage.input_tokens;self.stats['output_tokens']+=response.usage.output_tokens
                elif kind in ('openai','azure'):
                    if kind=='azure':client=openai.AzureOpenAI(api_key=key,max_retries=0,azure_endpoint=cfg.get('azure_endpoint','https://aidp.bytedance.net/api/modelhub/online/v2/crawl'),api_version=cfg.get('api_version','2024-02-01'),default_headers={'X-TT-LOGID':uuid.uuid4().hex},timeout=cfg.get('timeout',180))
                    else:client=openai.OpenAI(api_key=key,max_retries=0,base_url=cfg.get('base_url'),timeout=cfg.get('timeout',180))
                    params={k:cfg[k] for k in ('temperature','seed','reasoning_effort','max_completion_tokens') if k in cfg}
                    if 'max_tokens' in cfg and 'max_completion_tokens' not in params:params['max_completion_tokens']=cfg['max_tokens']
                    with client:response=client.chat.completions.create(model=cfg.get('model','openai/gpt-5' if kind=='azure' else 'gpt-5'),messages=[{'role':'user','content':prompt}],**params)
                    text=response.choices[0].message.content or '';self.metadata['actual_model']=response.model
                    if response.usage:self.stats['input_tokens']+=response.usage.prompt_tokens;self.stats['output_tokens']+=response.usage.completion_tokens
                else:raise ValueError('unknown proposer kind')
            except Exception as exc:raise ProposerError(f'API reflection failed: {type(exc).__name__}') from None
        if not isinstance(text,str) or not text.strip():raise ProposerError('Empty reflection response')
        self.stats['calls']+=1;self.stats['elapsed_ms']+=(time.perf_counter()-start)*1000
        return text

def make_proposer(config):return Proposer(config)
