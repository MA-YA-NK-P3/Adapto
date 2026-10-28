'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const X = require('../src/lib/extract.js');

test('parses LeetCode "N. Title" pattern into LC problem id', () => {
  assert.deepEqual(X.parseProblemTitle('21. Merge Two Sorted Lists'),
    { problemId: 'LC21', number: 21, title: 'Merge Two Sorted Lists' });
  assert.equal(X.parseProblemTitle('1. Two Sum - LeetCode').problemId, 'LC1');
  assert.equal(X.parseProblemTitle('  704. Binary Search  ').problemId, 'LC704');
});

test('rejects non-problem titles', () => {
  for (const t of ['', 'LeetCode', 'Two Sum', null, undefined, 42, 'Problems - LeetCode']) {
    assert.equal(X.parseProblemTitle(t), null, String(t));
  }
});

test('parses slug only from leetcode problem URLs', () => {
  assert.equal(X.parseSlug('https://leetcode.com/problems/two-sum/description/'), 'two-sum');
  assert.equal(X.parseSlug('https://leetcode.com/problemset/'), null);
  assert.equal(X.parseSlug('https://evil.example/problems/two-sum/'), null);
  assert.equal(X.parseSlug('not a url'), null);
});

test('normalizes difficulty labels', () => {
  assert.equal(X.normalizeDifficulty(' Easy '), 'EASY');
  assert.equal(X.normalizeDifficulty('Hard'), 'HARD');
  assert.equal(X.normalizeDifficulty('Extreme'), null);
  assert.equal(X.normalizeDifficulty(undefined), null);
});

test('maps submission verdicts to backend results', () => {
  assert.equal(X.mapSubmissionResult('Accepted'), 'ACCEPTED');
  assert.equal(X.mapSubmissionResult('Wrong Answer'), 'WRONG_ANSWER');
  assert.equal(X.mapSubmissionResult('Time Limit Exceeded'), 'TIME_LIMIT_EXCEEDED');
  assert.equal(X.mapSubmissionResult('Runtime Error'), 'RUNTIME_ERROR');
  assert.equal(X.mapSubmissionResult('Compile Error'), 'RUNTIME_ERROR');
  assert.equal(X.mapSubmissionResult('Memory Limit Exceeded'), 'RUNTIME_ERROR');
  assert.equal(X.mapSubmissionResult('Pending'), null);
  assert.equal(X.mapSubmissionResult(''), null);
});

const DATA = path.join(__dirname, '../../data');
const readJson = (p) => JSON.parse(fs.readFileSync(path.join(DATA, p), 'utf8'));

test('maps tags to tier-weighted concepts, strongest first, deduplicated, unknown dropped', () => {
  assert.deepEqual(X.mapTags(['Linked List', 'Recursion', 'Math', 'Recursion']), [
    { conceptCode: 'LINKED_LIST', weight: 1.0 },
    { conceptCode: 'RECURSION', weight: 1.0 },
    { conceptCode: 'MATH', weight: 0.5 },
  ]);
  assert.deepEqual(X.mapTags(['Stack', 'Queue']), [{ conceptCode: 'STACKS_QUEUES', weight: 1.0 }]);
  assert.deepEqual(X.mapTags(['Array', 'Hash Table']), [
    { conceptCode: 'HASHING', weight: 1.0 },
    { conceptCode: 'ARRAYS', weight: 0.71 },
  ]);
  assert.deepEqual(X.mapTags(['Simulation', 'Brainteaser']), []);
  assert.deepEqual(X.mapTags([]), []);
  assert.deepEqual(X.mapTags(undefined), []);
});

test('DFS/BFS are interpreted from context and Graph Theory maps to GRAPHS', () => {
  assert.deepEqual(X.mapTags(['Depth-First Search', 'Binary Tree']), [{ conceptCode: 'TREES', weight: 1.0 }]);
  assert.deepEqual(X.mapTags(['Breadth-First Search', 'Matrix', 'Array']).map((c) => c.conceptCode),
    ['GRAPHS', 'MATRIX', 'ARRAYS']);
  assert.deepEqual(X.mapTags(['Depth-First Search', 'String']), [
    { conceptCode: 'RECURSION', weight: 1.0 }, { conceptCode: 'STRINGS', weight: 0.71 }]);
  assert.deepEqual(X.mapTags(['Graph Theory']), [{ conceptCode: 'GRAPHS', weight: 1.0 }]);
  assert.equal(X.mapTags(['Array', 'String', 'Math', 'Sorting', 'Greedy', 'Hash Table']).length, 4);
});

test('mapping is identical to the catalog builder for every generated catalog problem', () => {
  const selection = readJson('leetcode/leetcode_problems_1000.json').problems;
  let checked = 0;
  for (const p of selection) {
    if (p.conceptSource !== 'generated') continue;
    const expected = Object.entries(p.concepts).map(([conceptCode, weight]) => ({ conceptCode, weight }));
    assert.deepEqual(X.mapTags(p.topicTags), expected, p.problemKey);
    checked++;
  }
  assert.equal(checked, 1000 - 28);
});

test('rule table matches data/leetcode/tag_concept_map.json and known concepts', () => {
  const map = readJson('leetcode/tag_concept_map.json');
  const codes = new Set(readJson('seed/catalog_1000.json').concepts.map((c) => c.code));
  const own = Object.entries(X.TAG_TO_CONCEPT);
  assert.equal(own.length, Object.keys(map.rules).length);
  for (const [tag, rule] of Object.entries(map.rules)) {
    assert.deepEqual(X.TAG_TO_CONCEPT[tag.toLowerCase()], { code: rule.concept, weight: rule.weight }, tag);
    assert.ok(codes.has(rule.concept), rule.concept);
  }
});

test('detects hint toggles and solution/editorial tabs', () => {
  for (const t of ['Hint 1', ' hint 2 ', 'Hint']) assert.equal(X.isHintToggle(t), true, t);
  for (const t of ['Hint 1: use a hash map', 'Hints are great', '', null]) assert.equal(X.isHintToggle(t), false, t);
  assert.equal(X.isSolutionPath('/problems/two-sum/solutions/'), true);
  assert.equal(X.isSolutionPath('/problems/two-sum/editorial'), true);
  assert.equal(X.isSolutionPath('/problems/two-sum/description/'), false);
  assert.equal(X.isSolutionPath('/problems/two-sum/submissions/123/'), false);
});

test('buildEvent produces a schema-v1 event and omits unobserved optional fields', () => {
  const ev = X.buildEvent({ userId: 'u1', problemId: 'LC21', eventId: 'e1', timestamp: '2026-01-01T00:00:00Z',
    result: 'WRONG_ANSWER', attemptNumber: 2, timeSpentSeconds: 61.6 });
  assert.deepEqual(ev, { userId: 'u1', problemId: 'LC21', eventId: 'e1', timestamp: '2026-01-01T00:00:00.000Z',
    attemptNumber: 2, result: 'WRONG_ANSWER', hintUsed: false, solutionViewed: false, timeSpentSeconds: 62 });
  assert.equal('confidence' in ev, false);
});

test('buildEvent rejects invalid input', () => {
  const ok = { userId: 'u', problemId: 'LC1', result: 'ACCEPTED' };
  assert.throws(() => X.buildEvent({ ...ok, userId: '' }), /userId/);
  assert.throws(() => X.buildEvent({ ...ok, problemId: 'two-sum' }), /problemId/);
  assert.throws(() => X.buildEvent({ ...ok, result: 'MAYBE' }), /result/);
  assert.throws(() => X.buildEvent({ ...ok, attemptNumber: 0 }), /attemptNumber/);
  assert.equal(X.buildEvent({ ...ok, timeSpentSeconds: -5 }).timeSpentSeconds, undefined);
});
