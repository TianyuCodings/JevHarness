/* Archived battle chapters + exact Jev answers. No inference is performed here. */
(() => {
  'use strict';
  const q = selector => document.querySelector(selector);
  const box = q('#short-demo'), frame = q('#highlight-replay');
  if (!box || !frame) return;
  const params = new URLSearchParams(location.search);
  const autoplay = params.get('autoplay') !== '0' &&
    (params.get('autoplay') === '1' || !matchMedia('(prefers-reduced-motion: reduce)').matches);
  const D = {key:null, data:null, session:null, token:0, loadToken:0, controller:null,
    index:0, phase:'loading', prepared:false, configured:false, intent:false,
    speed:1, resumePhase:null, timer:null, watchdog:null, visible:false, autoStarted:false,
    callNode:null, question:null, lastPosition:null, completedSegment:null};
  const chapter = () => D.data?.chapters[D.index];
  const clearTimer = () => {clearTimeout(D.timer); D.timer=null;};
  const setPhase = phase => {D.phase=phase; renderControls();};
  function command(cmd, extra={}) {
    const token=++D.token;
    frame.contentWindow?.postMessage({type:'auto-jev-highlight-command',version:1,
      session_id:D.session,token,cmd,...extra},'*');
    return token;
  }
  function cover(title, note='') {
    q('#short-cover').hidden=false;
    q('#short-cover-label').textContent=title;
    q('#short-cover-note').textContent=note;
    q('.short-probability').setAttribute('aria-busy','true');
    q('#short-inspect').disabled=true;
    q('#short-decision-title').textContent='Waiting for the matching decision';q('#short-node').textContent='—';q('#short-call-ms').textContent='—';
  }
  function fail(message) {
    clearTimer(); clearTimeout(D.watchdog); D.intent=false;
    if(D.configured)command('pause');
    setPhase('error'); cover('The replay could not be loaded.',message);
    q('#short-play').disabled=false; q('#short-play').textContent='Retry highlights';
  }
  async function setOverview(overview, force=false) {
    const run=overview?.run;
    if(!run?.run_id)return;
    const pair=(overview.paired_examples||[]).find(p=>p.episode_id==='pokemon-expanded-validation-05');
    const hash=pair?.after.hash||run.selected_hash;
    const key=[run.run_id,hash,pair?.after.history?.sha256].join('|');
    if(key===D.key&&!force)return;
    D.key=key;D.controller?.abort();D.controller=new AbortController();
    const load=++D.loadToken;clearTimer();clearTimeout(D.watchdog);
    D.data=null;D.index=0;D.token=0;D.prepared=false;D.configured=false;
    D.intent=false;D.autoStarted=false;D.callNode=null;D.question=null;
    D.session=crypto.randomUUID();frame.src='about:blank';
    q('#short-victory').hidden=true;q('#short-chapters').replaceChildren();
    q('#short-call-fields').replaceChildren();
    setPhase('loading');cover('Loading the featured moments…','Reading the archived decisions and their matching battle events.');
    const base='/api/run/'+encodeURIComponent(run.run_id)+'/game/'+encodeURIComponent(hash)+'/pokemon-expanded-validation-05';
    try {
      const data=await api(base+'/highlights?split=validation',D.controller.signal);
      if(load!==D.loadToken)return;
      if(!Array.isArray(data.chapters)||!data.chapters.length||data.binding?.candidate_hash!==hash||data.binding?.run_id!==run.run_id)throw new Error('The highlight archive does not match this experiment.');
      D.data=data;renderChapters();
      q('#short-progress').textContent=data.chapters.length+' featured moments · '+data.turns+'-turn battle';
      frame.src=base+'/replay?split=validation&highlights=1&highlight_session='+encodeURIComponent(D.session);
      D.watchdog=setTimeout(()=>fail('Renderer assets require a connection to play.pokemonshowdown.com. Retry or open the full archive below.'),35000);
    } catch(error) {
      if(error.name!=='AbortError'&&load===D.loadToken)fail(error.message);
    }
  }
  function renderChapters() {
    q('#short-chapters').replaceChildren(...D.data.chapters.map((c,index)=>{
      const button=el('button','short-chapter'+(index===D.index?' active':'')+(index<D.index||D.phase==='ended'?' completed':''));
      button.dataset.chapter=c.id;button.setAttribute('aria-pressed',String(index===D.index));
      button.append(el('span',null,String(index+1).padStart(2,'0')+' · TURN '+c.turn),el('strong',null,c.title));
      button.disabled=!D.configured;
      button.onclick=()=>{
        D.autoStarted=true;prepare(index,false);
        const rect=frame.getBoundingClientRect();
        if(rect.top<0||rect.bottom>innerHeight)frame.scrollIntoView({block:'center',behavior:'auto'});
      };return button;
    }));
  }
  function renderControls() {
    const names={loading:'Loading replay',preparing:'Positioning the battle',ready:'Ready to play',
      reading:'Decision preview',playing:'Playing the recorded move',paused:'Paused',
      between:'Move complete',ended:'Highlights complete',error:'Replay unavailable'};
    q('#short-state').textContent=names[D.phase]||D.phase;
    const play=q('#short-play');play.disabled=!D.configured||D.phase==='preparing';
    play.textContent=D.phase==='ended'?'Replay highlights':D.intent?'Pause':'Play highlights';
    q('#short-restart').disabled=!D.configured;
    q('#short-speed').disabled=!D.configured||D.phase==='preparing';
    if(D.data&&D.configured)q('#short-progress').textContent='Moment '+(D.index+1)+' / '+D.data.chapters.length+' · Turn '+chapter().turn;
  }
  function renderDecision() {
    const c=chapter(),d=c.decision,choice=choiceRecord(d),answer=choice?.answer;
    q('#short-title').textContent=c.title;
    q('#short-turn').textContent='TURN '+c.turn;
    q('#short-gap').textContent=c.gap_before?.caption||'';
    q('#short-caption').textContent=c.caption;
    q('#short-node').textContent=choice?.node.id||'Jev';
    q('#short-decision-title').textContent=answer?.choice?recordedActionLabel(d,answer.choice):'Recorded decision';
    q('#short-probabilities').replaceChildren(...Object.entries(answer?.probabilities||{}).sort((a,b)=>b[1]-a[1]).map(([id,p])=>{
      const row=el('div','answer-row'+(id===answer.choice?' chosen':'')),label=el('div','answer-label');
      row.dataset.action=id;
      label.append(el('span',null,recordedActionLabel(d,id)),el('strong',null,finite(p)?fmt(p*100)+'%':'Not recorded'));
      const track=el('div','answer-track'),bar=el('div','answer-bar');
      bar.style.width=(finite(p)?Math.max(0,Math.min(1,p))*100:0)+'%';track.append(bar);row.append(label,track);return row;
    }));
    q('#short-action').textContent=actionLabel(d);
    q('#short-gate').textContent=c.choice_usage.mode==='accepted_pick'?'The flow accepted Jev’s choice after its score check.':'The flow’s knockout rule selected this move; Jev recommended the same action.';
    const tactical=[];for(const node of jevNodes(d))for(const [id,a] of Object.entries(node.response?.answers||{}))if(a.type==='noul'){
      const chip=el('div','short-tactic');chip.append(el('span',null,human(id)),el('strong',null,finite(a.noul)?fmt(a.noul*100)+'%':'—'));tactical.push(chip);
    }
    q('#short-tactics').replaceChildren(...tactical);
    q('#short-call-ms').textContent=milliseconds(choice?.node.response?.elapsed_ms);
    q('#short-inspect').disabled=false;
    renderArguments();renderChapters();renderControls();
  }
  function renderArguments() {
    const c=chapter();if(!c||!D.prepared)return;
    const nodes=jevNodes(c.decision);
    if(!nodes.some(n=>n.id===D.callNode)){D.callNode=choiceRecord(c.decision)?.node.id||nodes[0]?.id;D.question=null;}
    q('#short-arguments-title').textContent='Turn '+c.turn+' · '+c.title;
    q('#short-call-tabs').replaceChildren(...nodes.map(node=>{
      const b=el('button',node.id===D.callNode?'active':null,node.id);b.dataset.node=node.id;
      b.setAttribute('aria-pressed',String(node.id===D.callNode));
      b.onclick=()=>{D.callNode=node.id;D.question=null;renderArguments();[...q('#short-call-tabs').querySelectorAll('button')].find(e=>e.dataset.node===node.id)?.focus({preventScroll:true});};return b;
    }));
    renderCallFields(q('#short-call-fields'),nodes.find(n=>n.id===D.callNode),c.decision,D.question,id=>{D.question=id;renderArguments();});
  }
  function prepare(index, playAfter) {
    if(!D.configured||!D.data?.chapters[index])return;
    clearTimer();D.index=index;D.prepared=false;D.intent=playAfter;D.completedSegment=null;
    D.callNode=null;D.question=null;q('#short-victory').hidden=true;
    q('#short-call-fields').replaceChildren();q('#short-arguments-title').textContent='Loading this moment’s exact call…';
    setPhase('preparing');renderChapters();
    const c=chapter();q('#short-title').textContent=c.title;q('#short-turn').textContent='TURN '+c.turn;q('#short-gap').textContent=c.gap_before?.caption||'';q('#short-caption').textContent='Preparing the recorded decision at turn '+c.turn+'.';cover('Turn '+c.turn+' · '+c.title,c.gap_before?.caption||'Aligning the battle position and recorded Jev answers.');
    command('prepare',{segment_id:c.id});
  }
  function preview() {
    if(!D.prepared||!D.intent)return;
    clearTimer();setPhase('reading');
    D.timer=setTimeout(()=>{
      D.timer=null;if(!D.intent)return;
      command('play',{segment_id:chapter().id});setPhase('playing');
    },2600/D.speed);
  }
  function play() {
    if(D.phase==='error'){setOverview(S.overview,true);return;}
    if(!D.configured)return;
    D.autoStarted=true;D.intent=true;
    if(D.phase==='ended'){prepare(0,true);return;}
    if(!D.prepared)return;
    if(D.phase==='paused'&&D.resumePhase==='playing'){
      command('play',{segment_id:chapter().id});setPhase('playing');
    }else if(D.phase==='between'||(D.phase==='paused'&&D.resumePhase==='between')){
      prepare(D.index+1,true);
    }else preview();
  }
  function pause() {
    clearTimer();D.intent=false;D.autoStarted=true;if(D.phase!=='paused')D.resumePhase=D.phase;
    if(!D.configured)return;
    if(D.phase==='playing'){command('pause');D.lastPosition={...D.lastPosition,phase:'playing'};}
    if(!['loading','preparing','ended','error'].includes(D.phase))setPhase('paused');
    else renderControls();
  }
  function maybeAutoplay() {
    if(autoplay&&!D.autoStarted&&D.visible&&D.prepared&&D.phase==='ready'&&!document.hidden){D.autoStarted=true;D.intent=true;preview();}
  }
  function completeSegment() {
    if(!D.prepared||D.completedSegment===chapter()?.id)return;
    D.completedSegment=chapter().id;clearTimer();
    if(D.index===D.data.chapters.length-1){
      D.intent=false;setPhase('ended');renderChapters();
      q('#short-victory-note').textContent='AutoJev · Turn '+D.data.turns;
      q('#short-victory').hidden=D.data.score!==1;
    }else{
      setPhase('between');
      if(D.intent)D.timer=setTimeout(()=>{D.timer=null;if(D.intent)prepare(D.index+1,true);},1400/D.speed);
    }
  }
  window.addEventListener('message',event=>{
    const m=event.data;
    if(event.source!==frame.contentWindow||m?.type!=='auto-jev-highlight'||m.version!==1||!D.data)return;
    if(m.frame_id!==D.session)return;
    if(m.event==='ready'){
      if(D.configured)return;
      command('configure',{binding:D.data.binding,replay_sha256:D.data.binding.replay_sha256,segments:D.data.chapters.map(c=>({id:c.id,start_step:c.start_step,end_step_exclusive:c.end_step_exclusive}))});return;
    }
    if(m.session_id!==D.session||m.token!==D.token)return;
    if(m.event==='error'){fail(m.message||m.error||'The recorded animation could not be synchronized.');return;}
    if(m.event==='configured'){
      clearTimeout(D.watchdog);D.configured=true;command('speed',{speed:D.speed});prepare(0,false);return;
    }
    if(m.segment_id&&m.segment_id!==chapter()?.id)return;
    if(m.event==='prepared'){
      D.prepared=true;D.lastPosition=m.position;q('#short-cover').hidden=true;
      q('.short-probability').setAttribute('aria-busy','false');
      setPhase('ready');renderDecision();
      if(D.intent)preview();else maybeAutoplay();return;
    }
    if(m.event==='position'){
      D.lastPosition=m.position;if(m.position?.phase==='segment-ended')completeSegment();return;
    }
    if(m.event==='segment-end'){D.lastPosition=m.position;completeSegment();}
  });
  q('#short-play').onclick=()=>D.intent?pause():play();
  q('#short-restart').onclick=()=>{D.autoStarted=true;prepare(0,true);};
  q('#short-speed').onchange=e=>{
    D.speed=Number(e.target.value);if(D.configured)command('speed',{speed:D.speed});
    if(D.phase==='reading')preview();
  };
  q('#short-inspect').onclick=()=>{pause();q('#short-arguments').open=true;q('#short-arguments').scrollIntoView({behavior:'smooth',block:'start'});};
  q('#short-arguments summary').onclick=()=>{if(!q('#short-arguments').open)pause();};
  q('#short-arguments').ontoggle=()=>{if(q('#short-arguments').open)renderArguments();};
  q('#short-full').onclick=()=>{
    pause();q('#paired-archive').open=true;S.pair='pokemon-expanded-validation-05';renderPairOptions();loadPair();
    q('#paired-archive').scrollIntoView({behavior:'smooth',block:'start'});
  };
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&D.intent)pause();});
  const observer=new IntersectionObserver(entries=>{D.visible=entries[0]?.isIntersecting||false;maybeAutoplay();},{threshold:.15});observer.observe(q('.short-frame-wrap'));
  window.HighlightDemo={setOverview,pause,getState:()=>({phase:D.phase,index:D.index,chapter:chapter()?.id,intent:D.intent,prepared:D.prepared,session:D.session,token:D.token,position:D.lastPosition})};
  if(S.overview)setOverview(S.overview);
})();
