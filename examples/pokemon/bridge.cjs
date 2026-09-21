'use strict';

// The trusted boundary is Showdown's per-player protocol stream, never Battle.sides.
const readline = require('node:readline');
const crypto = require('node:crypto');
const {BattleStream, getPlayerStreams, Dex, Teams, TeamValidator} = require('pokemon-showdown');

const FORMAT = 'gen9customgame@@@!! Max Team Size = 3,Min Team Size = 3,Terastal Clause';
const RULES = Object.freeze({generation: 9,game_type: 'singles',team_size: 3,
  format_kind: 'custom 3v3 pilot',engine_debug: false,opponent_hp: 'public percentage',
  terastallization: false,team_preview: 'fixed original order and lead'});
const clone = value => JSON.parse(JSON.stringify(value));
const toID = value => String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const identity = ident => `${String(ident).slice(0, 2)}: ${String(ident).split(': ').slice(1).join(': ')}`;
const blankBoosts = () => ({atk: 0,def: 0,spa: 0,spd: 0,spe: 0,accuracy: 0,evasion: 0});

function condition(text) {
  const tokens = String(text || '').split(' ');
  const hp = tokens[0].split('/').map(Number);
  const fainted = tokens.includes('fnt') || hp[0] === 0;
  return {hp_fraction: fainted ? 0 : hp.length === 2 && hp[1] > 0 ? hp[0] / hp[1] : null,
    hp_display: tokens[0],status: tokens.find(t => ['brn','par','slp','frz','psn','tox'].includes(t)) || null,fainted};
}

function speciesInfo(details) {
  const pieces = String(details || '').split(',').map(x => x.trim());
  const entry = Dex.species.get(pieces[0]);
  return {species: entry.exists ? entry.name : pieces[0],types: entry.exists ? [...entry.types] : [],
    level: Number((pieces.find(p => /^L\d+$/.test(p)) || 'L100').slice(1))};
}

function typeMultiplier(attackType, defenseTypes) {
  let multiplier = 1;
  for (const defenseType of defenseTypes || []) {
    const code = Dex.types.get(defenseType).damageTaken?.[attackType];
    multiplier *= code === 3 ? 0 : code === 1 ? 2 : code === 2 ? 0.5 : 1;
  }
  return multiplier;
}

function moveInfo(name) {
  const move = Dex.moves.get(name);
  return {id: move.id || toID(name),name: move.name || name,type: move.type || null,
    category: move.category || null,base_power: move.basePower || 0,
    accuracy: move.accuracy === true ? 1 : Number(move.accuracy || 0) / 100,
    priority: move.priority || 0,target: move.target || null,description: move.shortDesc || move.desc || '',
    boosts: clone(move.boosts || null),self: clone(move.self || null),status: move.status || null,
    volatile_status: move.volatileStatus || null,heal: clone(move.heal || null),
    recoil: clone(move.recoil || null),drain: clone(move.drain || null)};
}

class PlayerView {
  constructor(side) {
    this.side = side;
    this.other = side === 'p1' ? 'p2' : 'p1';
    this.turn = 0;
    this.history = [];
    this.publicTeams = {p1: [],p2: []};
    this.active = {p1: null,p2: null};
    this.typeStates = new WeakMap();
    this.field = {weather: null,weather_started_turn: null,conditions: [],side_conditions: {p1: [],p2: []}};
    this.request = null;
  }
  typeState(mon) {
    if (!this.typeStates.has(mon)) this.typeStates.set(mon, {
      base: speciesInfo(mon.species).types,change: null,added: null,roost: false,singleTurns: new Set(),
    });
    return this.typeStates.get(mon);
  }
  refreshTypes(mon) {
    const state = this.typeState(mon);
    let types = [...(state.change || state.base)];
    if (state.roost) types = types.filter(type => type !== 'Flying');
    if (!types.length) types = ['Normal'];
    if (state.added && !types.includes(state.added)) types.push(state.added);
    mon.types = types;
  }
  resetVolatile(mon) {
    if (!mon) return;
    mon.boosts = blankBoosts(); mon.effects = [];
    this.typeStates.delete(mon);
    this.refreshTypes(mon);
  }
  expireSingleTurns() {
    for (const mon of Object.values(this.active)) {
      if (!mon) continue;
      const state = this.typeState(mon);
      mon.effects = mon.effects.filter(effect => !state.singleTurns.has(effect));
      state.singleTurns.clear(); state.roost = false;
      this.refreshTypes(mon);
    }
  }
  member(ident, details = null) {
    const side = String(ident).slice(0, 2);
    if (!this.publicTeams[side]) return null;
    const key = identity(ident);
    const info = details === null ? null : speciesInfo(details);
    let mon = this.publicTeams[side].find(m => m.ident === key);
    if (!mon && info) mon = this.publicTeams[side].find(m => m.ident === null && m.species === info.species);
    if (!mon && info) {
      mon = {ident: key,...info,hp_fraction: null,hp_display: null,status: null,fainted: false,
        item: null,ability: null,revealed_moves: [],boosts: blankBoosts(),effects: []};
      this.publicTeams[side].push(mon);
    }
    if (mon) {
      mon.ident = key;
      if (info) {
        Object.assign(mon, info);
        this.typeState(mon).base = [...info.types];
        this.refreshTypes(mon);
      }
    }
    return mon || null;
  }
  parse(line) {
    if (!line.startsWith('|')) return;
    const [, event, ...args] = line.split('|');
    // Wall-clock timestamps are transport metadata, not battle state.
    if (event === 't:') return;
    if (event === 'request') {
      this.request = JSON.parse(args.join('|'));
      return;
    }
    // These are not normal player events; reject accidental unfiltered transport.
    if (event === 'split' || event === 'seed' || event === 'showteam' || event === 'debug') {
      throw new Error(`Forbidden player-view event: ${event}`);
    }
    this.history.push(line);
    if (event === 'upkeep' || event === 'turn') this.expireSingleTurns();
    if (event === 'turn') this.turn = Number(args[0]);
    if (event === 'clearpoke') {
      this.publicTeams = {p1: [],p2: []};
      this.active = {p1: null,p2: null}; this.typeStates = new WeakMap();
    }
    if (event === 'poke' && this.publicTeams[args[0]]) {
      this.publicTeams[args[0]].push({ident: null,...speciesInfo(args[1]),hp_fraction: null,hp_display: null,
        status: null,fainted: false,item: null,ability: null,revealed_moves: [],boosts: blankBoosts(),effects: []});
    }
    const mon = this.member(args[0]);
    if (['switch','drag','replace'].includes(event)) {
      // Switch-out clears volatile state immediately, including in bench views.
      this.resetVolatile(this.active[args[0].slice(0, 2)]);
      const target = this.member(args[0], args[1]);
      if (target) {
        this.resetVolatile(target);
        Object.assign(target, condition(args[2]));
        this.active[args[0].slice(0, 2)] = target;
      }
    } else if (['detailschange','-formechange'].includes(event) && mon) {
      const info = speciesInfo(args[1]);
      if (!/(?:^|,\s*)L\d+(?:,|$)/.test(args[1])) info.level = mon.level;
      Object.assign(mon, info);
      this.typeState(mon).base = [...info.types]; this.refreshTypes(mon);
    } else if (['-damage','-heal','-sethp'].includes(event)) {
      if (mon) Object.assign(mon, condition(args[1]));
      if (event === '-sethp' && args[2]) {
        const other = this.member(args[2]);
        if (other) Object.assign(other, condition(args[3]));
      }
    } else if (event === 'faint' && mon) {
      Object.assign(mon, {fainted: true,hp_fraction: 0,hp_display: '0'});
    } else if (event === '-status' && mon) mon.status = args[1];
    else if (event === '-curestatus' && mon) mon.status = null;
    else if (event === '-cureteam') {
      for (const entry of this.publicTeams[args[0].slice(0, 2)] || []) entry.status = null;
    } else if (event === '-ability' && mon) mon.ability = args[1];
    else if (event === '-item' && mon) mon.item = args[1];
    else if (event === '-enditem' && mon) mon.item = null;
    else if (event === 'move' && mon) {
      const from = args.find(a => a.startsWith('[from]')) || null;
      if (!mon.revealed_moves.some(m => m.id === toID(args[1]) && m.called_by === from)) {
        mon.revealed_moves.push({...moveInfo(args[1]),called_by: from});
      }
    } else if (['-boost','-unboost','-setboost'].includes(event) && mon && args[1] in mon.boosts) {
      const old = mon.boosts[args[1]], amount = Number(args[2]);
      mon.boosts[args[1]] = Math.max(-6, Math.min(6, event === '-setboost' ? amount : old + (event === '-unboost' ? -amount : amount)));
    } else if (event === '-clearboost' && mon) mon.boosts = blankBoosts();
    else if (event === '-clearallboost') {
      for (const entry of Object.values(this.active)) if (entry) entry.boosts = blankBoosts();
    } else if (event === '-singleturn' && mon) {
      const effect = args[1], state = this.typeState(mon);
      if (!mon.effects.includes(effect)) mon.effects.push(effect);
      state.singleTurns.add(effect);
      if (effect === 'move: Roost') { state.roost = true; this.refreshTypes(mon); }
    } else if (['-start','-end'].includes(event) && mon) {
      const effect = args[1];
      if (event === '-start' && !mon.effects.includes(effect)) mon.effects.push(effect);
      if (event === '-end') mon.effects = mon.effects.filter(e => e !== effect);
      if (effect === 'typechange') this.typeState(mon).change = event === '-start' ? args[2].split('/') : null;
      if (effect === 'typeadd') this.typeState(mon).added = event === '-start' ? args[2] : null;
      if (effect === 'typechange' || effect === 'typeadd') this.refreshTypes(mon);
    } else if (event === '-weather') {
      this.field.weather = args[0] === 'none' ? null : args[0];
      if (!args.includes('[upkeep]')) this.field.weather_started_turn = this.turn;
    } else if (['-fieldstart','-fieldend'].includes(event)) {
      if (event === '-fieldstart' && !this.field.conditions.includes(args[0])) this.field.conditions.push(args[0]);
      if (event === '-fieldend') this.field.conditions = this.field.conditions.filter(e => e !== args[0]);
    } else if (['-sidestart','-sideend'].includes(event)) {
      const target = this.field.side_conditions[args[0].slice(0, 2)];
      if (target && event === '-sidestart' && !target.includes(args[1])) target.push(args[1]);
      if (target && event === '-sideend') this.field.side_conditions[args[0].slice(0, 2)] = target.filter(e => e !== args[1]);
    }
    // Revealed abilities/items can also appear as causes on a public event.
    const ownerArg = args.find(a => a.startsWith('[of] '));
    const owner = ownerArg ? this.member(ownerArg.slice(5)) : mon;
    for (const arg of args) {
      if (owner && arg.startsWith('[from] ability: ')) owner.ability = arg.slice(16);
      if (owner && arg.startsWith('[from] item: ')) owner.item = arg.slice(13);
    }
  }
  observation() {
    const request = this.request;
    if (!request?.side || request.side.id !== this.side) throw new Error('Request does not belong to this player');
    const own = request.side.pokemon.map((p, i) => {
      const observed = this.member(p.ident);
      return {slot: i + 1,ident: identity(p.ident),...speciesInfo(p.details),...condition(p.condition),
        ...(observed ? {types: [...observed.types]} : {}),
        active: Boolean(p.active),stats: clone(p.stats || {}),moves: (p.moves || []).map(moveInfo),
        item: p.item || null,ability: p.ability || p.baseAbility || null,
        boosts: clone(observed?.boosts || blankBoosts()),effects: [...(observed?.effects || [])]};
    });
    const observation = {turn: this.turn,phase: request.forceSwitch?.[0] ? 'switch' : 'move',
      rules: RULES,own_team: own,active: own.find(p => p.active) || null,
      opponent_active: clone(this.active[this.other]),opponent_public_team: clone(this.publicTeams[this.other]),
      field: {weather: this.field.weather,weather_started_turn: this.field.weather_started_turn,
        conditions: [...this.field.conditions],side_conditions: {player: [...this.field.side_conditions[this.side]],
          opponent: [...this.field.side_conditions[this.other]]}},history: [...this.history]};
    observation.legal_actions = legalActions(request, observation);
    return observation;
  }
}

function legalActions(request, observation) {
  if (request.wait || request.teamPreview) return [];
  const active = request.active?.[0];
  if (active?.canTerastallize || active?.canMegaEvo || active?.canDynamax || active?.canZMove) {
    throw new Error('Unsupported transformation enabled; pilot format must disable it');
  }
  const actions = [];
  const forced = Boolean(request.forceSwitch?.[0]);
  if (forced || !active?.trapped) {
    for (const mon of observation.own_team) {
      if (!mon.active && !mon.fainted) actions.push({id: `switch:${mon.slot}`,kind: 'switch',
        label: `Switch to ${mon.species} (slot ${mon.slot})`,switch_slot: mon.slot,pokemon: clone(mon)});
    }
  }
  if (!forced) {
    for (const [index, move] of (active?.moves || []).entries()) {
      if (move.disabled || move.pp === 0) continue;
      const info = moveInfo(move.id || move.move);
      actions.push({id: `move:${index + 1}`,kind: 'move',label: `Use ${move.move} (slot ${index + 1})`,
        move_slot: index + 1,move: {...info,pp: move.pp ?? null,max_pp: move.maxpp ?? null,
          stab: observation.active?.types.includes(info.type) || false,
          type_multiplier: observation.opponent_active ? typeMultiplier(info.type, observation.opponent_active.types) : null}});
    }
  }
  if (!actions.length) throw new Error('Decision request has no supported legal actions');
  return actions;
}

function seededRng(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6D2B79F5) >>> 0;
    let value = Math.imul(state ^ state >>> 15, state | 1);
    value ^= value + Math.imul(value ^ value >>> 7, value | 61);
    return ((value ^ value >>> 14) >>> 0) / 4294967296;
  };
}

function chooseOpponent(observation, kind, rng, memory = {}) {
  const actions = observation.legal_actions;
  if (kind === 'random') return actions[Math.floor(rng() * actions.length)];
  const moves = actions.filter(a => a.kind === 'move');
  const switches = actions.filter(a => a.kind === 'switch');
  const moveScore = move => move.base_power * move.accuracy * (move.stab ? 1.5 : 1) * (move.type_multiplier ?? 1);
  if (kind === 'heuristic' && moves.length && observation.opponent_active && memory.lastSwitchTurn !== observation.turn - 1) {
    const attackTypes = observation.opponent_active.types;
    const risk = mon => Math.max(...attackTypes.map(type => typeMultiplier(type, mon.types)), 1);
    const currentRisk = risk(observation.active);
    const safer = switches.filter(a => risk(a.pokemon) < currentRisk && a.pokemon.hp_fraction > 0.25)
      .sort((a, b) => risk(a.pokemon) - risk(b.pokemon) || b.pokemon.hp_fraction - a.pokemon.hp_fraction);
    if (currentRisk >= 2 && safer.length && (observation.active.hp_fraction < 0.6 || moves.every(a => moveScore(a.move) < 70))) {
      memory.lastSwitchTurn = observation.turn;
      return safer[0];
    }
  }
  if (moves.length) return moves.reduce((best, action) => moveScore(action.move) > moveScore(best.move) ? action : best);
  return switches.reduce((best, action) => action.pokemon.hp_fraction > best.pokemon.hp_fraction ? action : best);
}

function validateEpisode(episode) {
  if (!episode || typeof episode.id !== 'string' || !episode.id || episode.id.length > 200) throw new Error('episode.id must be a nonempty string');
  if (episode.format !== FORMAT) throw new Error(`Pilot requires exact custom format: ${FORMAT}`);
  const seed = Array.isArray(episode.seed) ? episode.seed : String(episode.seed).split(',').map(Number);
  if (seed.length !== 4 || seed.some(n => !Number.isInteger(n) || n < 0 || n > 65535)) throw new Error('seed must contain four uint16 values');
  if (!['random','max_power','heuristic'].includes(episode.opponent)) throw new Error('Unknown opponent policy');
  for (const [field, defaultValue] of [['max_turns', 200],['max_requests', 600]]) {
    if (episode[field] !== undefined && (!Number.isInteger(episode[field]) || episode[field] < 1 || episode[field] > 10000)) throw new Error(`${field} must be an integer in [1,10000]`);
    episode[field] ??= defaultValue;
  }
  if (episode.opponent_seed !== undefined && (!Number.isInteger(episode.opponent_seed) || episode.opponent_seed < 0 || episode.opponent_seed > 0xFFFFFFFF)) throw new Error('opponent_seed must be uint32');
  const parsed = {};
  for (const name of ['player_team','opponent_team']) {
    if (typeof episode[name] !== 'string' || episode[name].length > 20000) throw new Error(`${name} must be bounded export text`);
    const team = Teams.import(episode[name]);
    if (!team || team.length !== 3) throw new Error(`${name} must contain exactly three Pokemon`);
    if (new Set(team.map(p => toID(p.species))).size !== 3) throw new Error('Pilot teams require distinct species');
    if (team.some(p => Dex.species.get(p.species).abilities && p.ability === 'Illusion')) throw new Error('Illusion is outside the pilot observation contract');
    const problems = new TeamValidator(FORMAT).validateTeam(team);
    if (problems?.length) throw new Error(`${name} validation failed: ${problems.join('; ')}`);
    parsed[name] = Teams.pack(team);
  }
  const opponentSeed = episode.opponent_seed ?? crypto.createHash('sha256').update(`opponent:${seed.join(',')}`).digest().readUInt32BE(0);
  return {...episode,seed: seed.join(','),...parsed,opponent_seed: opponentSeed};
}

class Bridge {
  constructor(emit, finish = () => {}) {
    this.emit = emit; this.finishCallback = finish;
    this.started = false; this.finished = false; this.ending = false;
    this.views = {p1: new PlayerView('p1'),p2: new PlayerView('p2')};
    this.spectator = []; this.pending = null; this.requestId = 0; this.totalRequests = 0;
    this.opponentMemory = {}; this.lastOpponentActions = new Set();
  }
  async command(message) {
    if (this.finished) throw new Error('Battle already terminated');
    if (message.cmd === 'start') {
      if (this.started) throw new Error('Battle already started');
      this.episode = validateEpisode(clone(message.episode));
      this.started = true; this.rng = seededRng(this.episode.opponent_seed);
      this.stream = new BattleStream(); this.channels = getPlayerStreams(this.stream);
      void this.pumpSpectator().catch(error => this.fail(error));
      void this.pumpPlayer('p1').catch(error => this.fail(error));
      void this.pumpPlayer('p2').catch(error => this.fail(error));
      // Custom Game defaults to debug:true, which reveals exact enemy HP and
      // debug calculations on PUBLIC channels. Disable it on a local format
      // copy before the engine starts; never alter the global Dex or Battle.
      const format = clone({...Dex.formats.get(this.episode.format, true),debug: false,battle: undefined});
      await this.stream.write(`>start ${JSON.stringify({formatid: this.episode.format,format,seed: this.episode.seed})}\n` +
        `>player p1 ${JSON.stringify({name: 'AutoJev',team: this.episode.player_team})}\n` +
        `>player p2 ${JSON.stringify({name: 'Opponent',team: this.episode.opponent_team})}`);
      return;
    }
    if (message.cmd !== 'act' || !this.started) throw new Error('Expected start or act command');
    if (!this.pending || message.request_id !== this.pending.request_id) throw new Error('Stale or duplicate request_id');
    const action = this.pending.actions.find(a => a.id === message.action_id);
    if (!action) throw new Error('action_id is not in the current legal action table');
    this.lastPlayerRequestId = this.pending.request_id;
    this.pending = null;
    await this.channels.p1.write(this.order(action));
  }
  order(action) {
    return action.kind === 'move' ? `move ${action.move_slot}` : `switch ${action.switch_slot}`;
  }
  async pumpSpectator() {
    for await (const chunk of this.channels.spectator) {
      if (chunk) this.spectator.push(...chunk.split('\n').filter(line => line && !line.startsWith('|t:|')));
    }
  }
  async pumpPlayer(side) {
    const view = this.views[side];
    for await (const chunk of this.channels[side]) {
      let requestSeen = false;
      for (const line of chunk.split('\n')) {
        if (this.finished) return;
        if (line.startsWith('|error|')) {
          const reason = line.slice(7);
          if (!reason.startsWith('[Unavailable choice]')) throw new Error(`${side} choice rejected: ${reason}`);
          if (side === 'p1') this.emit({type: 'action_rejected',request_id: this.lastPlayerRequestId,reason});
          continue; // Simulator sends a fresh request containing the newly revealed restriction.
        }
        view.parse(line);
        if (line.startsWith('|request|')) requestSeen = true;
        if (line.startsWith('|win|') || line === '|tie') {
          const name = line.slice(5);
          if (line !== '|tie' && !['AutoJev','Opponent'].includes(name)) throw new Error('Unexpected winner identity');
          if (!this.ending) {
            this.ending = true;
            setImmediate(() => this.complete(line === '|tie' ? 'draw' : name === 'AutoJev' ? 'player' : 'opponent'));
          }
        }
      }
      if (this.ending || !requestSeen || view.request.wait) continue;
      this.totalRequests++;
      if (view.turn > this.episode.max_turns || this.totalRequests > this.episode.max_requests) {
        this.ending = true;
        const reason = view.turn > this.episode.max_turns ? 'max_turns' : 'max_requests';
        setImmediate(() => this.complete(null, reason));
        continue;
      }
      if (view.request.teamPreview) {
        await this.channels[side].write('team 123');
        continue;
      }
      const observation = view.observation();
      if (side === 'p1') {
        const request_id = ++this.requestId;
        this.pending = {request_id,actions: observation.legal_actions};
        this.emit({type: 'observation',request_id,observation});
      } else {
        const action = chooseOpponent(observation, this.episode.opponent, this.rng, this.opponentMemory);
        await this.channels.p2.write(this.order(action));
      }
    }
    if (!this.finished && !this.ending) this.fail(new Error('Simulator stream ended without a terminal result'));
  }
  complete(winner, reason = null) {
    if (this.finished) return;
    this.finished = true; this.pending = null;
    const result = {type: 'result',status: reason ? 'truncated' : 'completed',winner,
      score: reason ? null : winner === 'player' ? 1 : winner === 'opponent' ? 0 : 0.5,
      turns: Math.max(this.views.p1.turn,this.views.p2.turn) - (reason === 'max_turns' ? 1 : 0),
      replay_log: this.spectator.join('\n')};
    if (reason) result.reason = reason;
    this.emit(result); this.stop(); this.finishCallback(0);
  }
  fail(error) {
    if (this.finished) return;
    this.finished = true; this.pending = null;
    // No engine dump, request object, seed, team export or stack is returned.
    this.emit({type: 'error',error: String(error?.message || 'Bridge failure').slice(0, 1500)});
    this.stop(); this.finishCallback(1);
  }
  stop() {
    if (this.stream) void this.stream.writeEnd().catch(() => {});
  }
}

function main() {
  const input = readline.createInterface({input: process.stdin,crlfDelay: Infinity});
  const bridge = new Bridge(value => process.stdout.write(`${JSON.stringify(value)}\n`), code => {
    process.exitCode = code; input.close(); process.stdin.pause();
  });
  let chain = Promise.resolve();
  input.on('line', line => {
    chain = chain.then(() => {
      if (line.length > 100000) throw new Error('Input line too large');
      return bridge.command(JSON.parse(line));
    }).catch(error => bridge.fail(error));
  });
  input.on('close', () => {
    void chain.then(() => {
      if (!bridge.finished) bridge.fail(new Error('Input closed before battle completed'));
    });
  });
}

module.exports = {FORMAT,RULES,PlayerView,Bridge,condition,legalActions,moveInfo,typeMultiplier,seededRng,chooseOpponent,validateEpisode};
if (require.main === module) main();
