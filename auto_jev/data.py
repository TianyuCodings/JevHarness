"""Public observations with explicit provenance and availability times."""
import hashlib
import html
import json
import math
import re
import time
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree
import httpx
from .storage import atomic_write_json


def _timestamp(value):
    if isinstance(value,(int,float)):return float(value)
    date=datetime.fromisoformat(value.replace('Z','+00:00'))
    if date.tzinfo is None:raise ValueError('Timestamps must include a timezone')
    return date.timestamp()


def fetch_coinbase(asset='BTC-USD',start=None,end=None,granularity=3600):
    if not re.fullmatch(r'[A-Z0-9]{2,15}-[A-Z0-9]{2,15}',asset):raise ValueError('Invalid market ID')
    if granularity not in (60,300,900,3600,21600,86400):raise ValueError('Unsupported granularity')
    start,end=_timestamp(start),_timestamp(end)
    if not start<end:raise ValueError('start must be before end')
    now=time.time();rows={};cursor=start
    with httpx.Client(timeout=30,headers={'User-Agent':'Auto-Jev-research/0.1'}) as client:
        while cursor<end:
            stop=min(end,cursor+granularity*299)
            response=client.get(f'https://api.exchange.coinbase.com/products/{asset}/candles',params={'granularity':granularity,'start':datetime.fromtimestamp(cursor,timezone.utc).isoformat(),'end':datetime.fromtimestamp(stop,timezone.utc).isoformat()})
            response.raise_for_status()
            for raw in response.json():
                if len(raw)!=6:raise ValueError('Unexpected candle schema')
                timestamp,low,high,opening,close,volume=map(float,raw)
                if start<=timestamp<end and timestamp+granularity<=min(now,end):
                    if not all(math.isfinite(x) for x in (timestamp,low,high,opening,close,volume)) or min(low,opening,close)<=0 or low>min(opening,close) or high<max(opening,close) or volume<0:raise ValueError('Invalid market candle')
                    rows[timestamp]={'timestamp':timestamp,'low':low,'high':high,'open':opening,'close':close,'volume':volume}
            cursor=stop
            if cursor<end:time.sleep(.15)
    bars=[rows[t] for t in sorted(rows)]
    if not bars:raise ValueError('No closed candles in range')
    digest=hashlib.sha256(json.dumps(bars,sort_keys=True).encode()).hexdigest()
    return {'id':f'{asset}-{int(start)}-{int(end)}','asset':asset,'interval_seconds':granularity,'bars':bars,'news':[],
            'provenance':{'synthetic':False,'source':'Coinbase Exchange public candles','retrieved_at':time.time(),'data_hash':digest,'missing_intervals':sum(max(0,int((b['timestamp']-a['timestamp'])/granularity)-1) for a,b in zip(bars,bars[1:])),'news_coverage':'none'}}


def fetch_rss(url):
    if not url.startswith(('https://','http://')):raise ValueError('RSS URL must use HTTP(S)')
    with httpx.Client(timeout=30,follow_redirects=True,headers={'User-Agent':'Auto-Jev-research/0.1'}) as client:
        with client.stream('GET',url) as response:
            response.raise_for_status();chunks=[];size=0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>5_000_000:raise ValueError('RSS response too large')
                chunks.append(chunk)
    received=time.time();root=ElementTree.fromstring(b''.join(chunks));result=[]
    for item in list(root.iter('item'))+list(root.iter('{http://www.w3.org/2005/Atom}entry')):
        fields={x.tag.split('}')[-1]:''.join(x.itertext()) for x in item}
        link=fields.get('link','')
        for child in item:
            if child.tag.endswith('link') and child.get('href'):link=child.get('href')
        rawtime=fields.get('pubDate') or fields.get('published') or fields.get('updated')
        try:published=_timestamp(rawtime)
        except (ValueError,TypeError,AttributeError):
            try:published=parsedate_to_datetime(rawtime).timestamp()
            except (ValueError,TypeError,AttributeError):published=None
        headline=html.unescape(fields.get('title',''))
        content=html.unescape(re.sub('<[^>]+>',' ',fields.get('encoded') or fields.get('content') or fields.get('description') or fields.get('summary','')))
        ident=fields.get('guid') or fields.get('id') or link or hashlib.sha256(headline.encode()).hexdigest()
        result.append({'id':ident,'published_at':published,'available_at':received,'received_at':received,'headline':headline,'content':content[:20000],'content_chars':len(content),'content_truncated':len(content)>20000,'source':url,'url':link,'timing_quality':'received','content_hash':hashlib.sha256(content.encode()).hexdigest(),'updated_at_source':fields.get('updated')})
    return result


def append_received_news(path, records):
    """Append changed revisions; repeated polling preserves the first arrival time."""
    import fcntl
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX);stream.seek(0)
        latest={}
        for line in stream:
            if line.strip():
                item=json.loads(line);latest[(item.get('source'),item['id'])]=item
        written=0
        for item in records:
            key=(item.get('source'),item['id']);previous=latest.get(key)
            fields=('headline','content_hash','content','published_at')
            if previous and all(previous.get(f)==item.get(f) for f in fields):continue
            stream.write(json.dumps(item,ensure_ascii=False)+'\n');latest[key]=item;written+=1
        stream.flush()
    return written


def save_episode(episode,path):atomic_write_json(Path(path),episode)

def load_episode(path):
    data=json.loads(Path(path).read_text())
    if not isinstance(data,(dict,list)):raise ValueError('Expected episode or list of episodes')
    return data


def demo_episodes():
    out=[]
    for k in range(6):
        bars=[];price=100.;start=1700000000+k*86400
        for i in range(16):
            opening=price;price*=1+(0.008 if k%3==0 else -0.006 if k%3==1 else math.sin(i)*.01)
            bars.append({'timestamp':start+i*3600,'open':opening,'high':max(opening,price)*1.001,'low':min(opening,price)*.999,'close':price,'volume':100.})
        out.append({'id':f'synthetic-{k}','asset':'SYNTHETIC-USD','interval_seconds':3600,'bars':bars,
                    'news':[{'id':f'synthetic-news-{k}','published_at':start,'available_at':start+3600,'headline':'Synthetic engineering event','content':'Simulated input; not historical news.','source':'synthetic','timing_quality':'synthetic'}],
                    'provenance':{'synthetic':True,'source':'deterministic engineering fixture'}})
    return out
