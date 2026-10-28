'use strict';
// Opt-in end-to-end test against a running backend: KT_LIVE=1 node --test tests/
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const X = require('../src/lib/extract.js');
const { ApiClient } = require('../src/lib/api.js');
const { EventQueue } = require('../src/lib/queue.js');

const base = process.env.KT_BASE_URL || 'http://localhost:8080';

test('extension pipeline registers an unknown problem and delivers its event', { skip: !process.env.KT_LIVE }, async () => {
  const client = new ApiClient({ baseUrl: base, apiKey: process.env.KT_API_KEY });
  const q = new EventQueue({ client });
  const userId = 'ext-live-' + crypto.randomUUID().slice(0, 8);
  const n = 90000 + Math.floor(Math.random() * 9999);
  const parsed = X.parseProblemTitle(`${n}. Live Extension Probe`);
  const event = X.buildEvent({ userId, problemId: parsed.problemId, eventId: crypto.randomUUID(), result: 'WRONG_ANSWER' });

  await q.enqueue({ event, problem: { problemKey: parsed.problemId, title: parsed.title, staticDifficulty: 'MEDIUM',
    concepts: X.mapTags(['Linked List']) } });
  const report = await q.flush();
  assert.equal(report.delivered, 1, JSON.stringify(report));

  await q.enqueue({ event });                  // replay of the same eventId
  assert.equal((await q.flush()).duplicates, 1);

  const state = (await client.state(userId)).body;
  assert.equal(state.totalEvents, 1);
  const next = (await client.nextAction(userId)).body;
  assert.ok(next.recommendation.problemId);
});
