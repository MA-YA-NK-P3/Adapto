/*
 * Pure extraction + normalization helpers (no DOM, no chrome.* APIs).
 * Identifies LeetCode problems, verdicts, tags, hints, and solution views.
 */
(function (root) {
  'use strict';

  const A = 1.0, B = 0.7, C = 0.5;
  const RULES = [
    ['ARRAYS', C, ['Array']],
    ['HASHING', B, ['Hash Table', 'Counting', 'Hash Function']],
    ['STRINGS', C, ['String']],
    ['STRINGS', A, ['String Matching', 'Rolling Hash', 'Knuth–Morris–Pratt Algorithm', 'Z Algorithm',
      'Boyer–Moore String-Search Algorithm', 'Suffix Array']],
    ['MATH', C, ['Math']],
    ['MATH', B, ['Number Theory', 'Combinatorics', 'Geometry', 'Greatest Common Divisor', 'Euclidean Algorithm',
      'Prime Factorization', 'Primality Test', 'Sieve Theory', 'Prime Number Sieve', 'Least Common Multiple',
      "Fermat's Little Theorem", 'Probability and Statistics']],
    ['DYNAMIC_PROGRAMMING', A, ['Dynamic Programming', 'Memoization', 'Knapsack Problem', '0-1 Knapsack',
      'Complete Knapsack', 'Longest Increasing Subsequence', 'DP on Trees']],
    ['SORTING', B, ['Sorting', 'Merge Sort', 'Quicksort', 'Counting Sort', 'Quickselect', 'Bucket Sort', 'Radix Sort']],
    ['GREEDY', A, ['Greedy']],
    ['BINARY_SEARCH', A, ['Binary Search']],
    ['BIT_MANIPULATION', A, ['Bit Manipulation', 'Bitmask']],
    ['PREFIX_SUM', A, ['Prefix Sum']],
    ['MATRIX', B, ['Matrix']],
    ['TWO_POINTERS', A, ['Two Pointers', "Floyd's Cycle Finding Algorithm"]],
    ['TREES', A, ['Tree', 'Binary Tree', 'Binary Search Tree', 'Binary Lifting']],
    ['HEAP', A, ['Heap (Priority Queue)']],
    ['GRAPHS', A, ['Graph Theory', 'Graph', 'Shortest Path', "Dijkstra's Algorithm", 'Topological Sort',
      'Directed Acyclic Graph', 'Bipartite Graph', 'Minimum Spanning Tree', 'Eulerian Circuit',
      'Strongly Connected Component', 'Biconnected Component']],
    ['STACKS_QUEUES', A, ['Stack', 'Queue', 'Bracket Sequences']],
    ['MONOTONIC_STACK', A, ['Monotonic Stack', 'Monotonic Queue']],
    ['SLIDING_WINDOW', A, ['Sliding Window']],
    ['DESIGN', A, ['Design', 'Data Stream', 'Iterator']],
    ['BACKTRACKING', A, ['Backtracking']],
    ['UNION_FIND', A, ['Union-Find']],
    ['SEGMENT_TREE', A, ['Segment Tree', 'Binary Indexed Tree', 'Ordered Set']],
    ['LINKED_LIST', A, ['Linked List', 'Doubly-Linked List']],
    ['RECURSION', A, ['Recursion']],
    ['RECURSION', B, ['Divide and Conquer']],
    ['TRIE', A, ['Trie']],
  ];
  const norm = (s) => String(s).trim().toLowerCase();
  const TAG_TO_CONCEPT = {};
  for (const [code, weight, tags] of RULES) {
    for (const t of tags) TAG_TO_CONCEPT[norm(t)] = { code, weight };
  }
  const TREE_TAGS = new Set(['tree', 'binary tree', 'binary search tree', 'dp on trees', 'binary lifting']);
  const GRAPH_CONTEXT_TAGS = new Set(['graph theory', 'graph', 'matrix', 'union-find', 'shortest path',
    'topological sort', 'directed acyclic graph', 'bipartite graph', 'minimum spanning tree']);
  const MAX_CONCEPTS_PER_PROBLEM = 4;

  const RESULT_PATTERNS = [
    [/^accepted\b/i, 'ACCEPTED'],
    [/wrong answer/i, 'WRONG_ANSWER'],
    [/time limit exceeded/i, 'TIME_LIMIT_EXCEEDED'],
    [/(runtime error|memory limit exceeded|compile error|output limit exceeded)/i, 'RUNTIME_ERROR'],
  ];

  /** "21. Merge Two Sorted Lists - LeetCode" -> { problemId: 'LC21', number: 21, title }
   *  "House Robber III - LeetCode" -> { problemId: null, number: null, title: 'House Robber III' } */
  function parseProblemTitle(text) {
    if (typeof text !== 'string') return null;
    const clean = text.replace(/\s+-\s+LeetCode$/, '').trim();
    if (!clean) return null;
    const m = clean.match(/^(\d{1,5})\.\s+(.+)$/);
    if (m) {
      return { problemId: 'LC' + Number(m[1]), number: Number(m[1]), title: m[2].trim() };
    }
    if (!clean.includes('LeetCode') && !clean.includes('Submission')) {
      return { problemId: null, number: null, title: clean };
    }
    return null;
  }

  /** https://leetcode.com/problems/two-sum/description/ -> 'two-sum' */
  function parseSlug(url) {
    try {
      const u = new URL(url);
      if (!/(^|\.)leetcode\.(com|cn)$/.test(u.hostname)) return null;
      const m = u.pathname.match(/^\/problems\/([a-z0-9-]+)/i);
      return m ? m[1].toLowerCase() : null;
    } catch (_) {
      return null;
    }
  }

  function normalizeDifficulty(text) {
    const t = String(text || '').trim().toUpperCase();
    return t === 'EASY' || t === 'MEDIUM' || t === 'HARD' ? t : null;
  }

  function mapSubmissionResult(text) {
    const t = String(text || '').trim();
    for (const [re, value] of RESULT_PATTERNS) {
      if (re.test(t)) return value;
    }
    return null;
  }

  function mapTags(tagNames) {
    const tags = (tagNames || []).map(norm);
    const hasAny = (set) => tags.some((t) => set.has(t));
    const weights = new Map();
    const put = (code, w) => weights.set(code, Math.max(weights.get(code) || 0, w));
    for (const t of tags) {
      if (t === 'depth-first search' || t === 'breadth-first search') {
        if (hasAny(TREE_TAGS)) put('TREES', A);
        else if (hasAny(GRAPH_CONTEXT_TAGS)) put('GRAPHS', A);
        else if (t === 'depth-first search') put('RECURSION', B);
        else put('GRAPHS', B);
      } else if (TAG_TO_CONCEPT[t]) {
        put(TAG_TO_CONCEPT[t].code, TAG_TO_CONCEPT[t].weight);
      }
      if (t === 'memoization') put('RECURSION', B);
    }
    if (weights.size === 0) return [];
    const top = Math.max(...weights.values());
    return [...weights.entries()]
      .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
      .slice(0, MAX_CONCEPTS_PER_PROBLEM)
      .map(([conceptCode, w]) => ({ conceptCode, weight: Math.round((w / top) * 100) / 100 }));
  }

  function isHintToggle(text) {
    return /^\s*hint\s*\d{0,2}\s*$/i.test(String(text || ''));
  }

  function isSolutionPath(pathname) {
    return /^\/problems\/[a-z0-9-]+\/(solutions|editorial)(\/|$)/i.test(String(pathname || ''));
  }

  const RESULTS = ['ACCEPTED', 'WRONG_ANSWER', 'TIME_LIMIT_EXCEEDED', 'RUNTIME_ERROR', 'ABANDONED'];

  function buildEvent(o) {
    if (!o || !o.userId) throw new Error('userId required');
    if (!o.problemId || typeof o.problemId !== 'string') throw new Error('invalid problemId');
    if (!RESULTS.includes(o.result)) throw new Error('invalid result');
    const attempt = o.attemptNumber === undefined || o.attemptNumber === null ? 1 : Number(o.attemptNumber);
    if (!Number.isInteger(attempt) || attempt < 1) throw new Error('invalid attemptNumber');
    const ev = {
      userId: String(o.userId),
      problemId: o.problemId,
      eventId: o.eventId,
      timestamp: new Date(o.timestamp || Date.now()).toISOString(),
      attemptNumber: attempt,
      result: o.result,
      hintUsed: Boolean(o.hintUsed),
      solutionViewed: Boolean(o.solutionViewed),
    };
    if (Number.isFinite(o.timeSpentSeconds) && o.timeSpentSeconds >= 0) {
      ev.timeSpentSeconds = Math.round(o.timeSpentSeconds);
    }
    if (o.sessionId) ev.sessionId = String(o.sessionId).slice(0, 64);
    return ev;
  }

  const api = {
    TAG_TO_CONCEPT, parseProblemTitle, parseSlug, normalizeDifficulty, mapSubmissionResult, mapTags,
    isHintToggle, isSolutionPath, buildEvent,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.KTExtract = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
