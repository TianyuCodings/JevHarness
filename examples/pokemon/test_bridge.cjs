'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const {FORMAT,RULES,Bridge,PlayerView,condition,legalActions,moveInfo,
  typeMultiplier,seededRng,chooseOpponent,validateEpisode} = require('./bridge.cjs');
const catalogue = require('./teams.json');
const copy = value => JSON.parse(JSON.stringify(value));
const episode = (overrides = {}) => ({id: 'engineering-only',format: FORMAT,seed: [1,2,3,4],
  player_team: catalogue.teams[0].team,opponent_team: catalogue.teams[1].team,
  opponent: 'max_power',max_turns: 60,...overrides});

function runBattle(overrides = {}, choose = obs => chooseOpponent(obs,'max_power',seededRng(3)), firstOnly = false) {
  return new Promise((resolve,reject) => {
    const events = [];
    let bridge;
    const timer = setTimeout(() => {
      bridge.fail(new Error('Engineering battle timed out'));
      reject(new Error('Engineering battle timed out'));
    }, 10000);
    bridge = new Bridge(event => {
      events.push(copy(event));
      if (event.type === 'observation') {
        if (firstOnly) {
          bridge.finished = true; bridge.stop(); clearTimeout(timer);
          resolve(event);
        } else {
          setImmediate(() => {
            if (!bridge.finished) bridge.command({cmd: 'act',request_id: event.request_id,
              action_id: choose(event.observation).id}).catch(error => bridge.fail(error));
          });
        }
      }
    }, code => {
      clearTimeout(timer);
      if (code) reject(new Error(events.at(-1).error));
      else resolve(events);
    });
    bridge.command({cmd: 'start',episode: episode(overrides)}).catch(error => bridge.fail(error));
  });
}

test('all four engineering catalogue teams pass the exact custom format validation', () => {
  assert.equal(catalogue.format, FORMAT);
  assert.equal(catalogue.teams.length, 4);
  for (const entry of catalogue.teams) {
    const validated = validateEpisode(episode({player_team: entry.team}));
    assert.equal(validated.seed,'1,2,3,4');
    assert.equal(typeof validated.player_team,'string');
    assert.equal(validated.player_team.includes('\n'),false);
  }
  assert.equal(RULES.engine_debug,false);
});

test('episode validation rejects unsupported format, team shape, seeds and limits', () => {
  assert.throws(() => validateEpisode(episode({format: 'gen9ou'})),/exact custom format/);
  for (const seed of [null, [], [0,1,2], [0,1,2,-1], [0,1,2,65536], [0,1,2,0.5]]) {
    assert.throws(() => validateEpisode(episode({seed})),/uint16/);
  }
  assert.throws(() => validateEpisode(episode({player_team: 'Pikachu\n- Thunderbolt'})),/exactly three/);
  assert.throws(() => validateEpisode(episode({opponent: 'peek_at_engine'})),/opponent/);
  assert.throws(() => validateEpisode(episode({opponent_seed: 2**32})),/uint32/);
  assert.throws(() => validateEpisode(episode({max_turns: 0})),/max_turns/);
});

test('public protocol tracks only revealed opponent fields and rejects private transports', () => {
  const view = new PlayerView('p1');
  view.parse('|poke|p2|Dragonite, L50, M|');
  view.parse('|switch|p2a: Dragonite|Dragonite, L50, M|100/100');
  assert.equal(view.active.p2.ability,null);
  assert.equal(view.active.p2.item,null);
  assert.deepEqual(view.active.p2.revealed_moves,[]);
  assert.equal('stats' in view.active.p2,false);
  view.parse('|turn|2');
  view.parse('|-boost|p2a: Dragonite|atk|1');
  view.parse('|-damage|p2a: Dragonite|63/100 brn');
  view.parse('|move|p2a: Dragonite|Dragon Claw|p1a: Charizard');
  view.parse('|-heal|p2a: Dragonite|70/100 brn|[from] item: Leftovers');
  assert.equal(view.active.p2.hp_fraction,0.7);
  assert.equal(view.active.p2.status,'brn');
  assert.equal(view.active.p2.boosts.atk,1);
  assert.equal(view.active.p2.item,'Leftovers');
  assert.deepEqual(view.active.p2.revealed_moves.map(m => m.id),['dragonclaw']);
  view.parse('|-weather|Sandstorm|[from] ability: Sand Stream|[of] p2a: Dragonite');
  assert.equal(view.active.p2.ability,'Sand Stream');
  assert.equal(view.field.weather,'Sandstorm');
  view.parse('|t:|123');
  assert.ok(!view.history.some(line => line.startsWith('|t:|')));
  for (const event of ['split','seed','showteam','debug']) {
    assert.throws(() => view.parse(`|${event}|secret`),/Forbidden/);
  }
  view.request = {side: {id: 'p2',pokemon: []}};
  assert.throws(() => view.observation(),/does not belong/);
});

function requestFor(pokemon, moves = ['Flamethrower','Air Slash']) {
  return {side: {id: 'p1',pokemon},active: [{moves: moves.map(move =>
    ({move,id: moveInfo(move).id,pp: 10,maxpp: 20}))}]};
}

test('switching out clears bench boosts and volatile effects on both sides', () => {
  const view = new PlayerView('p1');
  for (const side of ['p1','p2']) {
    view.parse(`|switch|${side}a: Dragonite|Dragonite, L50|100/100 brn`);
    view.parse(`|-boost|${side}a: Dragonite|atk|1`);
    view.parse(`|-boost|${side}a: Dragonite|spe|1`);
    view.parse(`|-start|${side}a: Dragonite|confusion`);
    view.parse(`|-start|${side}a: Dragonite|typechange|Water`);
    view.parse(`|switch|${side}a: Scizor|Scizor, L50|100/100`);
    const bench = view.publicTeams[side].find(mon => mon.species === 'Dragonite');
    assert.equal(bench.boosts.atk,0);
    assert.equal(bench.boosts.spe,0);
    assert.deepEqual(bench.effects,[]);
    assert.deepEqual(bench.types,['Dragon','Flying']);
    assert.equal(bench.status,'brn'); // Major status is not a volatile effect.
  }
  view.request = requestFor([
    {ident: 'p1: Scizor',details: 'Scizor, L50',condition: '100/100',active: true},
    {ident: 'p1: Dragonite',details: 'Dragonite, L50',condition: '100/100 brn',active: false},
  ],['Bullet Punch']);
  assert.equal(view.observation().own_team[1].boosts.atk,0);
});

test('public type changes drive own STAB and restore correctly when their explicit end arrives', () => {
  const view = new PlayerView('p1');
  view.parse('|switch|p1a: Charizard|Charizard, L50|153/153');
  view.parse('|switch|p2a: Dragonite|Dragonite, L50|100/100');
  view.request = requestFor([{ident: 'p1: Charizard',details: 'Charizard, L50',condition: '153/153',active: true}]);
  view.parse('|-start|p1a: Charizard|typechange|Water');
  let obs = view.observation();
  assert.deepEqual(obs.active.types,['Water']);
  assert.equal(obs.legal_actions.find(a => a.move?.id === 'flamethrower').move.stab,false);
  view.parse('|-start|p1a: Charizard|typeadd|Grass');
  assert.deepEqual(view.observation().active.types,['Water','Grass']);
  view.parse('|-end|p1a: Charizard|typeadd');
  assert.deepEqual(view.observation().active.types,['Water']);
  view.parse('|-end|p1a: Charizard|typechange|[silent]');
  obs = view.observation();
  assert.deepEqual(obs.active.types,['Fire','Flying']);
  assert.equal(obs.legal_actions.find(a => a.move?.id === 'flamethrower').move.stab,true);
  view.parse('|-start|p2a: Dragonite|typechange|Electric');
  view.parse('|-end|p2a: Dragonite|typechange|[silent]');
  assert.deepEqual(view.observation().opponent_active.types,['Dragon','Flying']);
  view.parse('|-formechange|p2a: Dragonite|Dragonite');
  assert.equal(view.observation().opponent_active.level,50);
});

test('Roost types stay correct during mid-turn switch requests and expire at upkeep', () => {
  const view = new PlayerView('p1');
  view.parse('|switch|p1a: Charizard|Charizard, L50|153/153');
  view.parse('|switch|p2a: Corviknight|Corviknight, L50|100/100');
  view.request = requestFor([
    {ident: 'p1: Charizard',details: 'Charizard, L50',condition: '153/153',active: true},
    {ident: 'p1: Garchomp',details: 'Garchomp, L50',condition: '183/183',active: false},
  ]);
  view.parse('|-singleturn|p1a: Charizard|move: Roost');
  view.parse('|-singleturn|p2a: Corviknight|move: Roost');
  let obs = view.observation();
  assert.deepEqual(obs.active.types,['Fire']);
  assert.deepEqual(obs.opponent_active.types,['Steel']);
  assert.equal(obs.legal_actions.find(a => a.move?.id === 'airslash').move.stab,false);
  view.request.forceSwitch = [true];
  obs = view.observation();
  assert.deepEqual(obs.active.types,['Fire']);
  assert.deepEqual(obs.opponent_active.types,['Steel']);
  assert.ok(obs.active.effects.includes('move: Roost'));
  view.parse('|upkeep');
  obs = view.observation();
  assert.deepEqual(obs.active.types,['Fire','Flying']);
  assert.deepEqual(obs.opponent_active.types,['Flying','Steel']);
  assert.ok(!obs.active.effects.includes('move: Roost'));
});

test('real engine Roost followed by slower U-turn exposes temporary types during the switch', {timeout: 5000}, async () => {
  const {BattleStream,getPlayerStreams,Dex,Teams,TeamValidator} = require('pokemon-showdown');
  const ours = Teams.import(catalogue.teams[0].team);
  const theirs = Teams.import(catalogue.teams[1].team);
  // Engineering-only team order: Scizor leads so its slower U-turn follows Roost.
  [theirs[0],theirs[1]] = [theirs[1],theirs[0]];
  for (const team of [ours,theirs]) assert.equal(new TeamValidator(FORMAT).validateTeam(team),null);
  const stream = new BattleStream(), channels = getPlayerStreams(stream);
  const views = {p1: new PlayerView('p1'),p2: new PlayerView('p2')};
  let reached = false;
  async function pump(side) {
    for await (const chunk of channels[side]) {
      let requested = false;
      for (const line of chunk.split('\n')) {
        views[side].parse(line);
        if (line.startsWith('|request|')) requested = true;
      }
      if (reached || !requested || views[side].request.wait) continue;
      const request = views[side].request;
      if (request.teamPreview) { await channels[side].write('team 123'); continue; }
      assert.ok(views[side].turn <= 2,'Engineering sequence failed to reach the expected switch');
      if (side === 'p2' && request.forceSwitch && views[side].turn === 2) {
        const obs = views[side].observation();
        assert.deepEqual(obs.opponent_active.types,['Fire']);
        assert.ok(obs.opponent_active.effects.includes('move: Roost'));
        reached = true; await stream.writeEnd(); continue;
      }
      await channels[side].write(side === 'p1' ? (views[side].turn === 1 ? 'move 2' : 'move 4') :
        (views[side].turn === 1 ? 'move 1' : 'move 3'));
    }
  }
  try {
    const reading = Promise.all([pump('p1'),pump('p2')]);
    const format = copy({...Dex.formats.get(FORMAT,true),debug: false,battle: undefined});
    await stream.write(`>start ${JSON.stringify({formatid: FORMAT,format,seed: '901,902,903,904'})}\n` +
      `>player p1 ${JSON.stringify({name: 'Engineering P1',team: Teams.pack(ours)})}\n` +
      `>player p2 ${JSON.stringify({name: 'Engineering P2',team: Teams.pack(theirs)})}`);
    await reading;
    assert.equal(reached,true);
  } finally { if (!reached) await stream.writeEnd(); }
});

test('legal actions respect forced switch, trapping, disabled slots and exhausted PP', () => {
  const own = [{slot: 1,active: true,fainted: false,types: ['Fire'],species: 'Charizard'},
    {slot: 2,active: false,fainted: false,types: ['Water'],species: 'Rotom-Wash'},
    {slot: 3,active: false,fainted: true,types: ['Dragon'],species: 'Garchomp'}];
  const obs = {own_team: own,active: own[0],opponent_active: {types: ['Grass','Steel']}};
  const request = {active: [{moves: [
    {id: 'flamethrower',move: 'Flamethrower',pp: 10,maxpp: 24},
    {id: 'roost',move: 'Roost',pp: 10,disabled: true},
    {id: 'airslash',move: 'Air Slash',pp: 0}]}]};
  const actions = legalActions(request,obs);
  assert.deepEqual(actions.map(a => a.id),['switch:2','move:1']);
  assert.equal(actions[1].move.type_multiplier,4);
  assert.equal(actions[1].move.stab,true);
  request.active[0].trapped = true;
  assert.deepEqual(legalActions(request,obs).map(a => a.id),['move:1']);
  request.forceSwitch = [true];
  assert.deepEqual(legalActions(request,obs).map(a => a.id),['switch:2']);
  assert.deepEqual(legalActions({wait: true},obs),[]);
  assert.throws(() => legalActions({active: [{canTerastallize: 'Fire'}]},obs),/transformation/);
});

test('static move and switch features use public type knowledge and explicit accuracy scale', () => {
  assert.equal(moveInfo('Flamethrower').accuracy,1);
  assert.equal(moveInfo('Hydro Pump').accuracy,0.8);
  assert.equal(moveInfo('Swords Dance').boosts.atk,2);
  assert.deepEqual(moveInfo('Roost').heal,[1,2]);
  assert.equal(typeMultiplier('Ground',['Flying','Steel']),0);
  assert.equal(typeMultiplier('Fire',['Grass','Steel']),4);
  assert.equal(condition('0 fnt').fainted,true);
  const a = seededRng(9876), b = seededRng(9876);
  assert.deepEqual(Array.from({length: 50},a),Array.from({length: 50},b));
});

test('first observation excludes hidden enemy configuration, seed and debug HP', async () => {
  const first = await runBattle({},undefined,true);
  const altered = await runBattle({opponent_team: catalogue.teams[1].team
    .replace('- Dragon Claw','- Dragon Pulse').replace('252 Atk / 4 SpD / 252 Spe','252 HP / 4 SpD / 252 Spe')},undefined,true);
  assert.deepEqual(first,altered);
  const obs = first.observation;
  assert.equal(obs.opponent_active.hp_display,'100/100');
  assert.equal(obs.opponent_active.item,null);
  assert.equal(obs.opponent_active.ability,null);
  assert.equal(obs.own_team.length,3);
  assert.ok(obs.active.stats.spa > 0);
  assert.equal(obs.opponent_public_team.length,3);
  assert.ok(!JSON.stringify(obs).includes('opponent_seed'));
  assert.ok(!obs.history.some(line => /^\|(request|debug|split|seed|showteam)\|/.test(line)));
});

test('same engine seed, independent opponent seed and actions reproduce all observations and public replay', async () => {
  const settings = {id: 'engineering-determinism',opponent: 'random',opponent_seed: 99};
  const first = await runBattle(settings);
  const second = await runBattle(settings);
  assert.deepEqual(first,second);
  const result = first.at(-1);
  assert.equal(result.status,'completed');
  assert.ok(['player','opponent','draw'].includes(result.winner));
  assert.ok([0,0.5,1].includes(result.score));
  assert.match(result.replay_log,/\|(win\||tie)/);
  assert.ok(!/\|(request|split|debug|seed|showteam)\|/.test(result.replay_log));
  for (const line of result.replay_log.split('\n')) {
    if (/^\|-(?:damage|heal)\|/.test(line) && !line.includes('|0 fnt')) {
      assert.match(line,/\|\d+\/100(?:\s|\||$)/);
    }
  }
});

test('bounded episodes return truncation without a fabricated engine draw', async () => {
  const byTurn = (await runBattle({id: 'engineering-turn-cap',max_turns: 1})).at(-1);
  assert.equal(byTurn.status,'truncated');
  assert.equal(byTurn.reason,'max_turns');
  assert.equal(byTurn.turns,1);
  assert.equal(byTurn.winner,null);
  assert.equal(byTurn.score,null);
  assert.ok(!byTurn.replay_log.split('\n').includes('|tie'));
  const byRequest = (await runBattle({id: 'engineering-request-cap',max_requests: 1})).at(-1);
  assert.equal(byRequest.status,'truncated');
  assert.equal(byRequest.reason,'max_requests');
  assert.equal(byRequest.score,null);
});

test('bridge rejects stale, duplicate and illegal choices without silently substituting actions', async () => {
  const bridge = new Bridge(() => {});
  bridge.started = true;
  bridge.channels = {p1: {write: async () => {}}};
  const action = {id: 'move:1',kind: 'move',move_slot: 1};
  bridge.pending = {request_id: 4,actions: [action]};
  await assert.rejects(bridge.command({cmd: 'act',request_id: 3,action_id: 'move:1'}),/Stale/);
  await assert.rejects(bridge.command({cmd: 'act',request_id: 4,action_id: 'move:99'}),/legal action/);
  await bridge.command({cmd: 'act',request_id: 4,action_id: 'move:1'});
  await assert.rejects(bridge.command({cmd: 'act',request_id: 4,action_id: 'move:1'}),/duplicate/);
});

test('unavailable choice refreshes the request ID and removes newly revealed illegal actions', async () => {
  const events = [];
  const bridge = new Bridge(event => events.push(event));
  bridge.started = true;
  bridge.episode = {max_turns: 60,max_requests: 600};
  const request = {side: {id: 'p1',pokemon: [
    {ident: 'p1: Charizard',details: 'Charizard, L50',condition: '153/153',active: true,moves: ['flamethrower']},
    {ident: 'p1: Rotom',details: 'Rotom-Wash, L50',condition: '157/157',active: false,moves: ['hydropump']} ]},
    active: [{moves: [{move: 'Flamethrower',id: 'flamethrower',pp: 24}]}]};
  bridge.channels = {p1: (async function* () {
    yield '|turn|1\n|request|' + JSON.stringify(request);
    bridge.lastPlayerRequestId = bridge.pending.request_id;
    bridge.pending = null;
    yield '|error|[Unavailable choice] Cannot switch: trapped';
    request.active[0].trapped = true;
    yield '|request|' + JSON.stringify(request);
    // Stop the fixture stream without fabricating a gameplay terminal event.
    bridge.ending = true;
  })()};
  await bridge.pumpPlayer('p1');
  assert.deepEqual(events.map(event => event.type),['observation','action_rejected','observation']);
  assert.equal(events[1].request_id,1);
  assert.equal(events[2].request_id,2);
  assert.ok(events[0].observation.legal_actions.some(a => a.kind === 'switch'));
  assert.ok(events[2].observation.legal_actions.every(a => a.kind === 'move'));
});

test('CLI uses JSONL errors and nonzero exit on malformed input or premature EOF', () => {
  const cli = path.join(__dirname,'bridge.cjs');
  for (const input of ['not JSON\n',JSON.stringify({cmd: 'act',request_id: 1,action_id: 'move:1'}) + '\n',
    JSON.stringify({cmd: 'start',episode: episode()}) + '\n']) {
    const result = spawnSync(process.execPath,[cli],{input,encoding: 'utf8',timeout: 10000});
    assert.equal(result.error,undefined);
    assert.equal(result.status,1);
    const lines = result.stdout.trim().split('\n').map(line => JSON.parse(line));
    assert.equal(lines.at(-1).type,'error');
    assert.ok(!lines.some(value => value.type === 'result'));
  }
});
