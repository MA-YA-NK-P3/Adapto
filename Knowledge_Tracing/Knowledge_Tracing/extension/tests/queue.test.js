'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventQueue, MemoryStorage, MAX_QUEUE } = require('../src/lib/queue.js');
const { ApiClient, ApiError } = require('../src/lib/api.js');

function fakeClient(script) {
  const calls = [];
  return {
    calls,
    async postEvent(ev) { calls.push(['event', ev.eventId]); return script.event(ev, calls); },
    async createProblem(p) { calls.push(['problem', p.problemKey]); return script.problem ? script.problem(p) : { status: 201 }; },
  };
}

const item = (id, extra = {}) => ({ event: { eventId: id, problemId: 'LC1' }, ...extra });

test('delivered events leave the queue', async () => {
  const q = new EventQueue({ client: fakeClient({ event: () => ({ status: 201, body: { duplicate: false } }) }) });
  await q.enqueue(item('a'));
  await q.enqueue(item('b'));
  assert.deepEqual(await q.flush(), { delivered: 2, duplicates: 0, dropped: 0, retrying: 0 });
  assert.equal(await q.size(), 0);
});

test('enqueue deduplicates by eventId', async () => {
  const q = new EventQueue({ client: fakeClient({ event: () => ({ status: 201, body: {} }) }) });
  assert.equal(await q.enqueue(item('a')), true);
  assert.equal(await q.enqueue(item('a')), false);
  assert.equal(await q.size(), 1);
});

test('server-side duplicates count as delivered', async () => {
  const q = new EventQueue({ client: fakeClient({ event: () => ({ status: 200, body: { duplicate: true } }) }) });
  await q.enqueue(item('a'));
  assert.equal((await q.flush()).duplicates, 1);
  assert.equal(await q.size(), 0);
});

test('network failures are retried with backoff, not lost', async () => {
  let now = 0;
  let fail = true;
  const client = fakeClient({ event: () => { if (fail) throw new TypeError('fetch failed'); return { status: 201, body: {} }; } });
  const q = new EventQueue({ client, now: () => now });
  await q.enqueue(item('a'));
  assert.equal((await q.flush()).retrying, 1);
  assert.equal(await q.size(), 1);
  fail = false;
  assert.equal((await q.flush()).retrying, 1, 'still backing off');
  now = 10_000;
  assert.equal((await q.flush()).delivered, 1);
  assert.equal(await q.size(), 0);
});

test('5xx is retried, 400 is dropped', async () => {
  const client = fakeClient({ event: (ev) => { throw new ApiError(ev.eventId === 'bad' ? 400 : 503, {}); } });
  const q = new EventQueue({ client });
  await q.enqueue(item('bad'));
  await q.enqueue(item('down'));
  const r = await q.flush();
  assert.equal(r.dropped, 1);
  assert.equal(r.retrying, 1);
  assert.deepEqual((await q.load()).map((i) => i.event.eventId), ['down']);
});

test('422 unknown problem registers the problem once and re-sends', async () => {
  let known = false;
  const client = fakeClient({
    event: () => { if (!known) throw new ApiError(422, { message: 'Unknown problem' }); return { status: 201, body: {} }; },
    problem: () => { known = true; return { status: 201 }; },
  });
  const q = new EventQueue({ client });
  await q.enqueue(item('a', { problem: { problemKey: 'LC9999', title: 't', staticDifficulty: 'EASY' } }));
  assert.equal((await q.flush()).delivered, 1);
  assert.deepEqual(client.calls, [['event', 'a'], ['problem', 'LC9999'], ['event', 'a']]);
});

test('422 treats 409 on registration as already registered', async () => {
  let n = 0;
  const client = fakeClient({
    event: () => { if (n++ === 0) throw new ApiError(422, {}); return { status: 201, body: {} }; },
    problem: () => { throw new ApiError(409, {}); },
  });
  const q = new EventQueue({ client });
  await q.enqueue(item('a', { problem: { problemKey: 'LC1' } }));
  assert.equal((await q.flush()).delivered, 1);
});

test('422 without registration payload is dropped', async () => {
  const q = new EventQueue({ client: fakeClient({ event: () => { throw new ApiError(422, {}); } }) });
  await q.enqueue(item('a'));
  assert.equal((await q.flush()).dropped, 1);
});

const unauthorized = (status = 401) => new ApiError(status, { error: 'UNAUTHORIZED', message: 'Missing or invalid API key' });

for (const status of [401, 403]) {
  test(`${status} keeps every item queued and records the auth error`, async () => {
    const client = fakeClient({ event: () => { throw unauthorized(status); } });
    const q = new EventQueue({ client, now: () => 5000 });
    await q.enqueue(item('a'));
    await q.enqueue(item('b'));
    const r = await q.flush();
    assert.deepEqual(r, { delivered: 0, duplicates: 0, dropped: 0, retrying: 0, held: 2,
      authError: { at: '1970-01-01T00:00:05.000Z' } });
    assert.deepEqual(client.calls, [['event', 'a']], 'stops at the first rejection');
    assert.deepEqual((await q.load()).map((i) => [i.event.eventId, i.tries]), [['a', 0], ['b', 0]]);
    assert.deepEqual(await q.authError(), { at: '1970-01-01T00:00:05.000Z' });
  });
}

test('flushing is paused while the auth error is set', async () => {
  const client = fakeClient({ event: () => { throw unauthorized(); } });
  const q = new EventQueue({ client });
  await q.enqueue(item('a'));
  await q.flush();
  await q.enqueue(item('b'));
  const r = await q.flush();
  assert.equal(r.held, 2);
  assert.ok(r.authError);
  assert.equal(client.calls.length, 1, 'no request while paused');
  assert.equal(await q.size(), 2);
});

test('clearing the auth error lets queued items deliver', async () => {
  let keyOk = false;
  const client = fakeClient({ event: () => { if (!keyOk) throw unauthorized(); return { status: 201, body: {} }; } });
  const q = new EventQueue({ client });
  await q.enqueue(item('a'));
  await q.flush();
  keyOk = true;
  await q.clearAuthError();
  assert.deepEqual(await q.flush(), { delivered: 1, duplicates: 0, dropped: 0, retrying: 0 });
  assert.equal(await q.size(), 0);
  assert.equal(await q.authError(), null);
});

test('a forced flush ignores the pause and clears the auth error on success', async () => {
  let keyOk = false;
  const client = fakeClient({ event: () => { if (!keyOk) throw unauthorized(); return { status: 201, body: {} }; } });
  const q = new EventQueue({ client });
  await q.enqueue(item('a'));
  await q.flush();
  assert.equal((await q.flush({ force: true })).held, 1, 'still rejected: stays paused');
  assert.ok(await q.authError());
  keyOk = true;
  assert.equal((await q.flush({ force: true })).delivered, 1);
  assert.equal(await q.authError(), null);
});

test('a forced flush with nothing queued clears a stale auth error', async () => {
  const q = new EventQueue({ client: fakeClient({ event: () => { throw unauthorized(); } }) });
  await q.enqueue(item('a'));
  await q.flush();
  await q.save([]);
  await q.flush({ force: true });
  assert.equal(await q.authError(), null);
});

test('401 while registering an unknown problem keeps the event', async () => {
  const client = fakeClient({
    event: () => { throw new ApiError(422, { message: 'Unknown problem' }); },
    problem: () => { throw unauthorized(); },
  });
  const q = new EventQueue({ client });
  await q.enqueue(item('a', { problem: { problemKey: 'LC9999' } }));
  assert.equal((await q.flush()).held, 1);
  assert.equal(await q.size(), 1);
  assert.ok(await q.authError());
});

test('other 4xx are still dropped and do not pause the queue', async () => {
  const client = fakeClient({ event: (ev) => { throw new ApiError(ev.eventId === 'bad' ? 400 : 404, {}); } });
  const q = new EventQueue({ client });
  await q.enqueue(item('bad'));
  await q.enqueue(item('gone'));
  assert.deepEqual(await q.flush(), { delivered: 0, duplicates: 0, dropped: 2, retrying: 0 });
  assert.equal(await q.size(), 0);
  assert.equal(await q.authError(), null);
});

test('queue is bounded', async () => {
  const q = new EventQueue({ storage: new MemoryStorage(), client: fakeClient({ event: () => ({}) }) });
  for (let i = 0; i < MAX_QUEUE + 5; i++) await q.enqueue(item('e' + i));
  assert.equal(await q.size(), MAX_QUEUE);
  assert.equal((await q.load())[0].event.eventId, 'e5', 'oldest dropped first');
});

test('ApiClient sends API key and surfaces error bodies', async () => {
  const seen = [];
  const fetchImpl = async (url, opts) => {
    seen.push({ url, opts });
    return { ok: false, status: 404, text: async () => JSON.stringify({ message: 'Learner not found: x' }) };
  };
  const c = new ApiClient({ baseUrl: 'http://api.test/', apiKey: 'k', fetchImpl });
  await assert.rejects(c.nextAction('x y'), (e) => e.status === 404 && /Learner not found/.test(e.message));
  assert.equal(seen[0].url, 'http://api.test/api/learners/x%20y/next-action');
  assert.equal(seen[0].opts.headers['X-API-Key'], 'k');
});
