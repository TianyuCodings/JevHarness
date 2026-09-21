/* Run with: node --test tests/test_highlight_replay.cjs
 * Execute the real adapter with a deterministic playback scheduler, not a game.
 * The fixture preserves the official scheduler's promise, [then], interruption,
 * and >300 ms yield ordering reviewed in:
 * https://github.com/smogon/pokemon-showdown-client/blob/master/play.pokemonshowdown.com/src/battle.ts
 */
'use strict';

const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const adapter = readFileSync(path.join(__dirname, '../examples/pokemon/static/highlight-replay.js'), 'utf8');

function deferred(resolved = false) {
  const callbacks = [];
  return {
    resolved,
    done(callback) {
      if (this.resolved) callback();
      else callbacks.push(callback);
      return this;
    },
    resolve() {
      if (this.resolved) return;
      this.resolved = true;
      for (const callback of callbacks.splice(0)) callback();
    },
  };
}

function fixture(actions, segments = [{id: 'one', start_step: 0, end_step_exclusive: actions.length}]) {
  const lines = actions.map((_, index) => `|fixture|${index}`);
  const raw = lines.join('\n');
  const messages = [], microtasks = [], frames = [], timers = [];
  const listeners = new Map(), animations = [];
  let now = 0, token = 0;
  const stats = {batches: 0, overlappingBatches: 0, forcedFinishes: 0, queueEnds: 0, flushSteps: []};
  const scene = {
    animating: true, interruptionCount: 1, active: [], timeOffset: 0,
    startAnimations() {
      if (this.active.some(animation => !animation.resolved)) stats.overlappingBatches++;
      stats.batches++;
      this.active = [];
      this.timeOffset = 0;
    },
    finishAnimations() {
      stats.flushSteps.push(battle.currentStep);
      if (!this.active.length) return undefined;
      const waiting = this.active.filter(animation => !animation.resolved);
      const result = deferred(waiting.length === 0);
      let remaining = waiting.length;
      for (const animation of waiting) animation.done(() => {
        if (--remaining === 0) result.resolve();
      });
      return result;
    },
    stopAnimation(explicitReplacement = false) {
      this.interruptionCount++;
      for (const animation of this.active) {
        if (!animation.resolved && !explicitReplacement) stats.forcedFinishes++;
        animation.resolve();
      }
    },
    pause() { this.stopAnimation(); },
    resume() {},
    animationOff() { this.stopAnimation(true); this.animating = false; },
    animationOn() { this.animating = true; },
    updateAcceleration() { this.acceleration = 1; },
  };
  const battle = {
    scene, stepQueue: lines, currentStep: 0, turn: 0, paused: true,
    seeking: null, atQueueEnd: false, ended: false, waitForAnimations: true,
    shouldStep() {
      if (this.atQueueEnd) return false;
      return this.seeking !== null || !this.paused;
    },
    run() {
      const index = this.currentStep, action = actions[index];
      this.turn = index + 1;
      if (action.win) this.ended = true;
      if (!scene.animating) return;
      now += action.cost || 0;
      if (action.kind === 'then') this.waitForAnimations = false;
      if (action.kind === 'simult') this.waitForAnimations = 'simult';
      if (action.kind && action.kind !== 'none') {
        const animation = deferred(action.kind === 'sync');
        animations.push({index, animation});
        scene.active.push(animation);
      }
    },
    nextStep() {
      if (!this.shouldStep()) return;
      const startedAt = now;
      scene.startAnimations();
      let promise;
      do {
        this.waitForAnimations = true;
        if (this.currentStep >= lines.length) {
          this.atQueueEnd = true;
          stats.queueEnds++;
          this.stopSeeking();
          return;
        }
        this.run(lines[this.currentStep]);
        this.currentStep++;
        if (this.waitForAnimations === true) promise = scene.finishAnimations();
        else if (this.waitForAnimations === 'simult') scene.timeOffset = 0;
        const interruption = scene.interruptionCount;
        if (now - startedAt > 300) {
          timers.push(() => { if (interruption === scene.interruptionCount) this.nextStep(); });
          return; // The official yield precedes registration of promise.done.
        }
      } while (!promise && this.shouldStep());
      if (this.paused && this.seeking === null) return scene.pause();
      if (promise) {
        const interruption = scene.interruptionCount;
        promise.done(() => { if (interruption === scene.interruptionCount) this.nextStep(); });
      }
    },
    pause() { this.paused = true; scene.pause(); },
    play() { this.paused = false; scene.resume(); this.nextStep(); },
    stopSeeking() { this.seeking = null; scene.animationOn(); },
    seekTurn(target) {
      this.seeking = target;
      scene.animationOff();
      this.currentStep = 0;
      this.turn = 0;
      this.atQueueEnd = false;
      this.ended = false;
      scene.active = [];
      this.nextStep();
    },
    setMute() {},
  };
  const parent = {postMessage(message, origin) {
    assert.equal(origin, 'https://fixture.local');
    messages.push(message);
  }};
  const context = {
    URL, URLSearchParams, TextEncoder, Uint8Array, Map, Set,
    location: {href: 'https://fixture.local/replay?highlights=1&highlight_session=fixture', search: '?highlights=1&highlight_session=fixture'},
    parent, window: {Replays: {battle}}, innerWidth: 642, innerHeight: 362,
    document: {
      querySelector(selector) { return selector === 'script.battle-log-data' ? {textContent: raw} : null; },
      createElement() { return {}; }, head: {append() {}},
    },
    crypto: {subtle: {async digest(_, bytes) { return createHash('sha256').update(bytes).digest(); }}},
    Date: {now: () => now},
    addEventListener(name, listener) { listeners.set(name, listener); },
    queueMicrotask(callback) { microtasks.push(callback); },
    requestAnimationFrame(callback) { frames.push(callback); },
    setTimeout(callback) { timers.push(callback); },
    setInterval() {},
  };
  vm.runInNewContext(adapter, context, {filename: 'highlight-replay.js'});

  async function flush() {
    // Native async command continuations and the adapter's controlled microtasks.
    for (let pass = 0; pass < 100; pass++) {
      await Promise.resolve();
      if (!microtasks.length) {
        await Promise.resolve();
        if (!microtasks.length) return;
      }
      for (const callback of microtasks.splice(0)) callback();
    }
    throw Error('Adapter did not reach a microtask boundary');
  }
  async function send(cmd, extra = {}) {
    listeners.get('message')({source: parent, origin: 'https://fixture.local', data: {
      type: 'auto-jev-highlight-command', version: 1, session_id: 'fixture', token: ++token, cmd, ...extra,
    }});
    await flush();
  }
  async function paint() {
    for (let frame = 0; frame < 2; frame++) {
      for (const callback of frames.splice(0)) callback();
      await flush();
    }
  }
  return {
    battle, scene, stats, messages, animations, send, flush, paint,
    events(name) { return messages.filter(message => message.event === name); },
    async initialize({offscreen = false} = {}) {
      await send('configure', {replay_sha256: createHash('sha256').update(raw).digest('hex'), segments});
      assert.equal(this.events('configured').length, 1);
      await send('prepare', {segment_id: segments[0].id});
      if (offscreen) await this.runTimers();
      else await paint();
      assert.equal(this.events('prepared').length, 1);
      stats.batches = stats.overlappingBatches = stats.forcedFinishes = 0;
      stats.flushSteps = [];
    },
    async resolve(index) {
      const entry = animations.findLast(item => item.index === index && !item.animation.resolved);
      assert.ok(entry, `Expected a pending animation for fixture line ${index}`);
      entry.animation.resolve();
      await flush();
    },
    async runTimers() { for (const callback of timers.splice(0)) callback(); await flush(); },
    assertHealthy() {
      assert.equal(this.events('error').length, 0, JSON.stringify(this.events('error')));
      assert.equal(stats.forcedFinishes, 0, 'A natural boundary must not finish an active animation');
      assert.equal(stats.overlappingBatches, 0, 'A new batch must wait for the previous animation');
    },
  };
}

test('a native >300 ms continuation cannot start a second batch before the animation drains', async () => {
  const f = fixture([{kind: 'async', cost: 301}, {kind: 'async'}, {kind: 'none', win: true}]);
  await f.initialize();
  await f.send('play');
  assert.equal(f.battle.currentStep, 1);
  await f.runTimers();
  assert.equal(f.battle.currentStep, 1);
  assert.equal(f.stats.batches, 1);
  assert.equal(f.battle.paused, false);
  await f.resolve(0);
  assert.equal(f.battle.currentStep, 2);
  assert.equal(f.stats.batches, 2);
  await f.resolve(1);
  assert.equal(f.battle.currentStep, 3);
  assert.equal(f.stats.queueEnds, 1);
  assert.equal(f.events('segment-end').length, 0, 'Completion waits for the paint acknowledgement');
  await f.paint();
  const [ended] = f.events('segment-end');
  assert.equal(f.events('segment-end').length, 1);
  assert.equal(ended.position.current_step, 3);
  assert.equal(ended.position.ended, true);
  assert.equal(ended.position.animation_pending, false);
  f.assertHealthy();
});

test('synchronous done is safe and a later [then] in the same batch receives exactly one final flush', async () => {
  const f = fixture([{kind: 'sync'}, {kind: 'none'}, {kind: 'then'}]);
  await f.initialize();
  await f.send('play');
  assert.equal(f.battle.currentStep, 3);
  assert.deepEqual(f.stats.flushSteps, [1, 2, 3]);
  assert.equal(f.battle.paused, false, 'The final then animation is still active');
  assert.equal(f.events('segment-end').length, 0);
  await f.resolve(2);
  await f.paint();
  assert.equal(f.events('segment-end').length, 1);
  assert.equal(f.stats.flushSteps.filter(step => step === 3).length, 1);
  f.assertHealthy();
});

for (const cancelResume of [false, true]) {
  test(`play during a graceful pause's paint window is ${cancelResume ? 'cancelled by a later pause' : 'resumed after paint'}`, async () => {
    const f = fixture([{kind: 'async'}, {kind: 'async'}]);
    await f.initialize();
    await f.send('play');
    await f.send('pause');
    assert.equal(f.battle.paused, false, 'Pause must first drain the live animation');
    await f.resolve(0);
    assert.equal(f.battle.currentStep, 1);
    assert.equal(f.battle.paused, true);
    await f.send('play');
    assert.equal(f.battle.currentStep, 1, 'Resume is queued while the acknowledgement is painting');
    if (cancelResume) await f.send('pause');
    await f.paint();
    assert.equal(f.battle.currentStep, cancelResume ? 1 : 2);
    assert.equal(f.battle.paused, cancelResume);
    if (cancelResume) await f.send('play');
    await f.resolve(1);
    await f.paint();
    assert.equal(f.events('segment-end').length, 1);
    f.assertHealthy();
  });
}

test('superseded prepare paint and native continuation callbacks cannot advance or acknowledge a new epoch', async () => {
  const f = fixture([{kind: 'async', cost: 301}, {kind: 'none'}, {kind: 'async'}], [
    {id: 'old', start_step: 0, end_step_exclusive: 2},
    {id: 'new', start_step: 2, end_step_exclusive: 3},
  ]);
  await f.initialize();
  await f.send('play');
  assert.equal(f.battle.currentStep, 1);
  await f.send('prepare', {segment_id: 'old'}); // Leave its two-frame acknowledgement pending.
  await f.send('prepare', {segment_id: 'new'});
  await f.runTimers(); // The old native 300 ms callback is also still queued.
  await f.paint();
  assert.deepEqual(f.events('prepared').map(event => event.segment_id), ['old', 'new']);
  assert.equal(f.battle.currentStep, 2);
  assert.equal(f.battle.paused, true);
  await f.send('play', {segment_id: 'new'});
  assert.equal(f.battle.currentStep, 3);
  assert.equal(f.battle.paused, false);
  assert.equal(f.events('segment-end').length, 0);
  await f.resolve(2);
  await f.paint();
  assert.deepEqual(f.events('segment-end').map(event => event.segment_id), ['new']);
  f.assertHealthy();
});

test('offscreen timer acknowledgements wait for animation drain and late frames cannot duplicate or supersede them', async () => {
  const f = fixture([{kind: 'async', cost: 301}, {kind: 'async', win: true}], [
    {id: 'old', start_step: 0, end_step_exclusive: 1},
    {id: 'new', start_step: 1, end_step_exclusive: 2},
  ]);
  await f.initialize({offscreen: true}); // No requestAnimationFrame callback runs.
  assert.equal(f.events('prepared')[0].view_sync, 'dom-ready');
  await f.send('play');
  await f.runTimers(); // Includes the native >300 ms continuation while still animating.
  assert.equal(f.battle.currentStep, 1);
  assert.equal(f.battle.paused, false, 'An offscreen timeout must not finish the live animation');
  assert.equal(f.events('segment-end').length, 0);
  await f.resolve(0);
  assert.equal(f.events('segment-end').length, 0);
  await f.runTimers();
  assert.equal(f.events('segment-end').length, 1);
  assert.equal(f.events('segment-end')[0].view_sync, 'dom-ready');
  assert.equal(f.events('segment-end')[0].position.animation_pending, false);

  await f.send('prepare', {segment_id: 'old'});
  await f.send('prepare', {segment_id: 'new'}); // Supersede old paint AND fallback callbacks.
  await f.runTimers();
  assert.deepEqual(f.events('prepared').map(event => event.segment_id), ['old', 'new']);
  assert.ok(f.events('prepared').every(event => event.view_sync === 'dom-ready'));
  await f.send('play', {segment_id: 'new'});
  await f.paint(); // Deliver all delayed frames while the NEW animation is pending.
  assert.equal(f.battle.currentStep, 2);
  assert.equal(f.battle.paused, false);
  assert.equal(f.events('prepared').length, 2);
  assert.equal(f.events('segment-end').length, 1);
  await f.runTimers();
  assert.equal(f.battle.paused, false);
  assert.equal(f.events('segment-end').length, 1);
  await f.resolve(1);
  await f.runTimers();
  assert.deepEqual(f.events('segment-end').map(event => event.segment_id), ['old', 'new']);
  assert.ok(f.events('segment-end').every(event => event.view_sync === 'dom-ready'));
  await f.paint();
  await f.runTimers();
  assert.equal(f.events('prepared').length, 2);
  assert.equal(f.events('segment-end').length, 2);
  assert.equal(f.stats.queueEnds, 1);
  f.assertHealthy();
});
