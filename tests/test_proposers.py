from types import SimpleNamespace as NS
import pytest
from auto_jev.providers import make_proposer

@pytest.mark.parametrize('kind,key_name',[('azure','OPENAI_API_KEY'),('openai','OPENAI_DIRECT_API_KEY')])
def test_api_provider_configuration_is_forwarded(monkeypatch,kind,key_name):
    import openai
    seen={}
    class Client:
        def __init__(self,**kw):
            seen['client']=kw
            self.chat=NS(completions=NS(create=self.create))
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kw):
            seen['request']=kw
            return NS(model='reported-model',choices=[NS(message=NS(content='{"ok":true}'))],usage=NS(prompt_tokens=10,completion_tokens=4))
    monkeypatch.setattr(openai,'AzureOpenAI',Client)
    monkeypatch.setattr(openai,'OpenAI',Client)
    monkeypatch.setenv(key_name,'FAKE-key-for-test')
    p=make_proposer({'kind':kind,'api_key_env':key_name,'model':'configured-model','max_tokens':128})
    assert p('test')=='{"ok":true}'
    assert seen['client']['api_key']=='FAKE-key-for-test'
    assert seen['request']['model']=='configured-model'
    assert seen['request']['max_completion_tokens']==128
    assert 'temperature' not in seen['request'] and 'seed' not in seen['request']
    assert p.metadata['actual_model']=='reported-model'
    assert p.stats['input_tokens']==10
    if kind=='azure':assert seen['client']['azure_endpoint']=='https://aidp.bytedance.net/api/modelhub/online/v2/crawl'


def test_anthropic_effort_is_forwarded(monkeypatch):
    import anthropic
    seen={}
    class Client:
        def __init__(self,**kw):self.messages=NS(create=self.create)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kw):
            seen.update(kw)
            return NS(model='claude-fable-5-1',content=[NS(type='text',text='{}')],usage=NS(input_tokens=2,output_tokens=1))
    monkeypatch.setattr(anthropic,'Anthropic',Client);monkeypatch.setenv('ANTHROPIC_API_KEY','FAKE-key')
    p=make_proposer({'kind':'anthropic','model':'claude-fable-5-1','effort':'xhigh'})
    assert p('test')=='{}'
    assert seen['output_config']=={'effort':'xhigh'}
