/* Adapt the official public replay renderer to reviewed protocol-line segments.
 * This script does not simulate games or calculate any model answers.
 *
 * Parent commands: {type:'auto-jev-highlight-command', version:1, session_id,
 * token, cmd:'configure'|'prepare'|'play'|'pause'|'speed', segment_id?, ...}.
 * Configure adds binding, replay_sha256 and segments with id/start_step/
 * end_step_exclusive. Indices address the original log.split('\n'); currentStep
 * is the next unread line. All replies use type:'auto-jev-highlight', version:1.
 * A command token must increase within its session. Ready is independent of
 * configuration; only configured acknowledges the asynchronous SHA check.
 */
(() => {
  'use strict';
  const params = new URLSearchParams(location.search);
  if (params.get('highlights') !== '1') return;
  const frameId = params.get('highlight_session');
  const parentOrigin = new URL(location.href).origin;
  const S = {
    battle: null, session: null, token: -1, epoch: 0, configured: false,
    segments: new Map(), segment: null, target: null, phase: 'booting',
    mode: null, desiredPlaying: false, pauseRequested: false, finalizing: false,
    pending: new Set(), depth: 0, batch: null, runGeneration: 0,
    resumeRequested: false, resumeAfterSettle: false, settlingKind: null,
    checkScheduled: false, allowQueueEnd: false, speed: 1,
  };
  let original;

  function position() {
    const b = S.battle;
    return {
      current_step: b ? b.currentStep : null, turn: b ? b.turn : null,
      paused: b ? b.paused : true, ended: b ? b.ended : false,
      phase: S.phase, animation_pending: S.pending.size > 0,
      speed: S.speed, start_step: S.segment?.start_step ?? null,
      end_step_exclusive: S.segment?.end_step_exclusive ?? null,
    };
  }
  function emit(event, extra = {}) {
    parent.postMessage({type: 'auto-jev-highlight', version: 1, event,
      frame_id: frameId, session_id: S.session, token: S.token,
      segment_id: S.segment?.id ?? null, position: position(), ...extra}, parentOrigin);
  }
  function fail(message) {
    // Stop requesting more steps, without finishing a live animation early.
    S.desiredPlaying = false;
    S.pauseRequested = true;
    S.phase = 'error';
    emit('error', {error: message});
    scheduleCheck();
  }
  function fit() {
    const wrapper = document.querySelector('.wrapper');
    const battle = document.querySelector('.battle');
    if (!wrapper || !battle) return;
    const width = battle.offsetWidth || 642, height = battle.offsetHeight || 362;
    const scale = Math.min(innerWidth / width, innerHeight / height);
    wrapper.style.cssText = `position:absolute;left:${(innerWidth-width*scale)/2}px;top:${(innerHeight-height*scale)/2}px;width:${width}px;height:${height}px;margin:0;transform-origin:0 0;transform:scale(${scale});`;
  }
  function scheduleCheck() {
    if (S.checkScheduled) return;
    S.checkScheduled = true;
    queueMicrotask(() => {
      S.checkScheduled = false;
      try { check(); } catch (error) { fail('Replay boundary control failed: ' + String(error.message || error)); }
    });
  }

  // finishAnimations returns a jQuery promise covering its registered effect,
  // sprite and delay queues. currentStep advances BEFORE that promise resolves.
  // Track each flush, including synchronous resolution and the final [then]
  // group for which the native loop does not call finishAnimations itself.
  function trackedFinish() {
    const epoch = S.epoch;
    if (S.batch) S.batch.flushedGeneration = S.runGeneration;
    const result = original.finishAnimations();
    if (result && typeof result.done === 'function') {
      const marker = {};
      S.pending.add(marker);
      result.done(() => {
        if (epoch !== S.epoch) return;
        S.pending.delete(marker);
        scheduleCheck();
      });
    }
    return result;
  }
  function afterPaint(epoch, callback) {
    let completed = false;
    const finish = synchronization => {
      if (completed) return;
      completed = true;
      if (epoch === S.epoch) callback(synchronization);
    };
    requestAnimationFrame(() => requestAnimationFrame(() => finish('animation-frames')));
    // Chromium suspends rAF in an iframe scrolled outside the viewport. At
    // this point every registered animation has ALREADY drained and the DOM
    // has been reconstructed. A bounded DOM-ready acknowledgement therefore
    // remains accurate without claiming that an invisible frame was painted.
    // This timeout never runs while an action's animation queue is pending.
    setTimeout(() => finish('dom-ready'), 150);
  }
  function settle(kind) {
    const b = S.battle, epoch = S.epoch;
    S.finalizing = true;
    S.settlingKind = kind;
    S.resumeRequested = false;
    if (kind === 'prepared') {
      // Seeking runs without animation. animationOn reconstructs the exact
      // public view at the next-unread-line boundary before we acknowledge it.
      b.stopSeeking();
    } else if (kind === 'segment-end' && S.target === b.stepQueue.length && !b.atQueueEnd) {
      // Execute the renderer's queue-end bookkeeping only after the last win
      // animation has drained, and before paused makes native shouldStep false.
      S.allowQueueEnd = true;
      try { original.nextStep(); } finally { S.allowQueueEnd = false; }
    }
    original.pause(); // Safe here: every registered animation has completed.
    fit();
    afterPaint(epoch, synchronization => {
      S.finalizing = false;
      S.settlingKind = null;
      S.pauseRequested = false;
      S.desiredPlaying = false;
      S.mode = null;
      S.phase = kind === 'segment-end' ? 'segment-ended' : kind;
      if (S.resumeAfterSettle && kind === 'paused') {
        S.resumeAfterSettle = false;
        startPlayback();
        return;
      }
      S.resumeAfterSettle = false;
      emit(kind === 'paused' ? 'position' : kind, {view_sync: synchronization});
    });
  }
  function check() {
    if (!S.battle || S.depth || S.pending.size || S.finalizing) return;
    const atTarget = S.target !== null && S.battle.currentStep >= S.target;
    if ((S.mode && atTarget) || S.pauseRequested) {
      if (S.batch && S.batch.flushedGeneration !== S.runGeneration) {
        trackedFinish();
        if (S.pending.size) return;
      }
      if (S.mode === 'prepare' && atTarget) settle('prepared');
      else if (S.mode === 'play' && atTarget) settle('segment-end');
      else settle('paused');
      return;
    }
    if (S.resumeRequested && (S.mode === 'prepare' || S.desiredPlaying)) {
      S.battle.nextStep();
    }
  }
  function install(b) {
    S.battle = b;
    const scene = b.scene;
    original = {
      shouldStep: b.shouldStep.bind(b), nextStep: b.nextStep.bind(b),
      run: b.run.bind(b), pause: b.pause.bind(b),
      startAnimations: scene.startAnimations.bind(scene),
      finishAnimations: scene.finishAnimations.bind(scene),
      updateAcceleration: scene.updateAcceleration.bind(scene),
    };
    original.pause();
    b.setMute(true);
    // The embed's autoresize scales .battle against the iframe width. The
    // wrapper below already performs that scale (and vertical centering), so
    // retaining both would shrink mobile battles twice.
    if (b.autoresize && typeof b.onResize === 'function') {
      removeEventListener('resize', b.onResize);
      b.autoresize = false;
    }
    b.shouldStep = () => {
      if (S.allowQueueEnd && b.currentStep === b.stepQueue.length) return true;
      if (S.finalizing || S.target === null || b.currentStep >= S.target) return false;
      if (S.mode === 'prepare') return original.shouldStep();
      return S.mode === 'play' && S.desiredPlaying && !S.pauseRequested && original.shouldStep();
    };
    b.nextStep = () => {
      // Native nextStep has a 300 ms yielding path which can re-enter before
      // its current animation promise resolves. Never begin another batch then.
      if (S.pending.size || S.depth || S.finalizing) {
        S.resumeRequested = true;
        return;
      }
      S.resumeRequested = false;
      S.depth++;
      try { return original.nextStep(); }
      finally { S.depth--; scheduleCheck(); }
    };
    b.run = (...args) => { S.runGeneration++; return original.run(...args); };
    scene.startAnimations = () => {
      S.batch = {flushedGeneration: -1};
      return original.startAnimations();
    };
    scene.finishAnimations = trackedFinish;
    scene.updateAcceleration = () => {
      original.updateAcceleration();
      // The renderer may elide effects at >= 3. Preserve its full animations.
      scene.acceleration = S.speed;
    };
    scene.updateAcceleration();
    const style = document.createElement('style');
    style.textContent = 'html,body{margin:0!important;width:100%;height:100%;overflow:hidden;background:#e8edf0}.notice,#load-status,.replay-controls,.replay-controls-2,.battle-log,.playbutton{display:none!important}.battle{position:absolute!important;left:0!important;top:0!important;margin:0!important;transform:none!important}';
    document.head.append(style);
    addEventListener('resize', fit);
    fit();
    S.phase = 'ready';
    emit('ready', {replay_line_count: b.stepQueue.length});
    setInterval(() => { if (S.configured) emit('position'); }, 200);
  }
  function invalidate() {
    S.epoch++;
    S.pending = new Set();
    S.batch = null;
    S.finalizing = false;
    S.resumeRequested = false;
    S.resumeAfterSettle = false;
    S.settlingKind = null;
    S.desiredPlaying = false;
    S.pauseRequested = false;
    S.mode = null;
    S.target = null;
  }
  async function configure(data) {
    invalidate();
    original.pause(); // Explicit replacement is allowed to cancel the old view.
    S.configured = false;
    S.segment = null;
    S.segments = new Map();
    S.phase = 'configuring';
    const epoch = S.epoch;
    const raw = document.querySelector('script.battle-log-data')?.textContent;
    const expected = data.replay_sha256 || data.binding?.replay_sha256;
    if (typeof raw !== 'string' || !/^[a-f0-9]{64}$/.test(expected || '')) throw Error('A public replay SHA-256 is required');
    const lines = raw.split('\n');
    if (S.battle.stepQueue.length !== lines.length || lines.some((line, index) => line !== S.battle.stepQueue[index])) throw Error('Renderer protocol lines differ from the archived replay');
    if (data.binding?.replay_line_count !== undefined && data.binding.replay_line_count !== lines.length) throw Error('Replay line count mismatch');
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw)))].map(n => n.toString(16).padStart(2, '0')).join('');
    if (epoch !== S.epoch) return;
    if (hash !== expected || (data.binding?.replay_sha256 && hash !== data.binding.replay_sha256)) throw Error('Replay SHA-256 mismatch');
    if (!Array.isArray(data.segments) || !data.segments.length) throw Error('At least one reviewed segment is required');
    const segments = new Map();
    for (const segment of data.segments) {
      if (!segment || typeof segment.id !== 'string' || !segment.id || segments.has(segment.id) ||
          !Number.isSafeInteger(segment.start_step) || !Number.isSafeInteger(segment.end_step_exclusive) ||
          segment.start_step < 0 || segment.start_step >= segment.end_step_exclusive || segment.end_step_exclusive > lines.length) throw Error('Invalid replay segment boundaries');
      segments.set(segment.id, {id: segment.id, start_step: segment.start_step, end_step_exclusive: segment.end_step_exclusive});
    }
    S.segments = segments;
    S.configured = true;
    S.phase = 'configured';
    emit('configured', {replay_sha256: hash, replay_line_count: lines.length});
  }
  function requireSegment(data) {
    if (!S.configured) throw Error('Replay configuration has not been verified');
    if (!S.segment || (data.segment_id !== undefined && data.segment_id !== S.segment.id)) throw Error('The command does not match the prepared segment');
  }
  function startPlayback() {
    S.target = S.segment.end_step_exclusive;
    S.mode = 'play';
    S.phase = 'playing';
    S.pauseRequested = false;
    S.desiredPlaying = true;
    S.battle.play();
    emit('position');
  }
  async function command(data) {
    if (!S.battle) return;
    if (typeof data.session_id !== 'string' || !data.session_id || !Number.isSafeInteger(data.token) || data.token < 0) return;
    if (data.cmd === 'configure') {
      if (S.session === data.session_id && data.token <= S.token) return;
      S.session = data.session_id;
      S.token = data.token;
      await configure(data);
      return;
    }
    if (data.session_id !== S.session || data.token <= S.token) return;
    S.token = data.token;
    if (data.cmd === 'prepare') {
      if (!S.configured || !S.segments.has(data.segment_id)) throw Error('Unknown or unverified segment');
      const segment = S.segments.get(data.segment_id);
      invalidate();
      S.segment = segment;
      S.target = segment.start_step;
      S.mode = 'prepare';
      S.phase = 'preparing';
      S.battle.seekTurn(Infinity, true);
      scheduleCheck();
    } else if (data.cmd === 'play') {
      requireSegment(data);
      if (S.finalizing && S.settlingKind === 'paused') {
        // The user may resume during the two paint frames after a graceful
        // pause. Retain that intent without interrupting the completed batch.
        S.resumeAfterSettle = true;
        return;
      }
      if (S.mode === 'prepare' || S.finalizing) throw Error('Wait for the prepared acknowledgement');
      if (S.battle.currentStep >= S.segment.end_step_exclusive) throw Error('Prepare this segment again to replay it');
      startPlayback();
    } else if (data.cmd === 'pause') {
      requireSegment(data);
      S.resumeAfterSettle = false;
      if (S.mode === 'prepare' || S.finalizing) return;
      S.desiredPlaying = false;
      S.pauseRequested = true;
      S.phase = 'pausing';
      scheduleCheck();
      emit('position');
    } else if (data.cmd === 'speed') {
      if (data.speed !== 1 && data.speed !== 2) throw Error('Supported playback speeds are 1 and 2');
      S.speed = data.speed;
      S.battle.scene.updateAcceleration();
      emit('position');
    } else throw Error('Unknown replay command');
  }
  addEventListener('message', event => {
    if (event.source !== parent || event.origin !== parentOrigin || !event.data ||
        event.data.type !== 'auto-jev-highlight-command' || event.data.version !== 1) return;
    const data = event.data;
    command(data).catch(error => {
      if (data.session_id === S.session && data.token === S.token) fail(String(error.message || error));
    });
  });
  const deadline = Date.now() + 45000;
  function boot() {
    const b = window.Replays?.battle;
    if (b?.scene && Array.isArray(b.stepQueue) && b.stepQueue.length &&
        ['shouldStep', 'nextStep', 'run', 'pause', 'stopSeeking', 'seekTurn'].every(name => typeof b[name] === 'function') &&
        ['startAnimations', 'finishAnimations', 'updateAcceleration'].every(name => typeof b.scene[name] === 'function')) {
      try { install(b); } catch (error) { fail('Official replay renderer is incompatible: ' + String(error.message || error)); }
    } else if (Date.now() < deadline) setTimeout(boot, 50);
    else fail('Official replay renderer did not become ready');
  }
  boot();
})();
