/*
 * Durable outbound event queue.
 *   - events are persisted before sending (survive service-worker restarts / offline)
 *   - dedupe by eventId; backend idempotency makes re-sends safe
 *   - 201/200 -> delivered; 422 unknown problem -> register problem once, then retry;
 *     401/403 -> kept, auth error recorded and flushing paused until it is cleared
 *     (settings changed) or a forced flush succeeds; other 4xx -> dropped (retrying
 *     cannot fix a bad payload); network/5xx -> kept, exponential backoff
 */
(function (root) {
  'use strict';

  const MAX_QUEUE = 500;
  const MAX_BACKOFF_MS = 15 * 60 * 1000;

  class MemoryStorage {
    constructor() { this.data = {}; }
    async get(key) { return this.data[key]; }
    async set(key, value) { this.data[key] = JSON.parse(JSON.stringify(value)); }
  }

  const isAuthError = (err) => Boolean(err) && (err.status === 401 || err.status === 403);

  class EventQueue {
    constructor({ storage, client, now = () => Date.now(), key = 'kt.queue', authKey = 'kt.authError' }) {
      this.storage = storage || new MemoryStorage();
      this.client = client;
      this.now = now;
      this.key = key;
      this.authKey = authKey;
    }

    async load() { return (await this.storage.get(this.key)) || []; }
    async save(items) { await this.storage.set(this.key, items); }
    async size() { return (await this.load()).length; }

    /** { at } while the backend rejects the API key (401/403), otherwise null. */
    async authError() { return (await this.storage.get(this.authKey)) || null; }
    async clearAuthError() { await this.storage.set(this.authKey, null); }

    /** item: { event, problem? } — problem is the registration payload for unknown problems. */
    async enqueue(item) {
      const items = await this.load();
      if (items.some((i) => i.event.eventId === item.event.eventId)) return false;
      items.push({ ...item, tries: 0, nextAttemptAt: 0 });
      while (items.length > MAX_QUEUE) items.shift();
      await this.save(items);
      return true;
    }

    backoff(tries) {
      return Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, tries));
    }

    /**
     * While an auth error is recorded nothing is sent and the report carries `authError`
     * and `held`; `force` ignores the pause (manual retry).
     */
    async flush({ force = false } = {}) {
      const items = await this.load();
      const keep = [];
      const report = { delivered: 0, duplicates: 0, dropped: 0, retrying: 0 };
      const paused = await this.authError();
      if (paused && !force) return { ...report, held: items.length, authError: paused };
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.nextAttemptAt > this.now()) {
          keep.push(item);
          report.retrying++;
          continue;
        }
        const outcome = await this.sendOne(item);
        if (outcome === 'auth') {
          // the same key would be rejected for every remaining item: keep them all untouched
          keep.push(...items.slice(i));
          report.held = items.length - i;
          report.authError = { at: new Date(this.now()).toISOString() };
          await this.storage.set(this.authKey, report.authError);
          break;
        }
        if (outcome === 'delivered' || outcome === 'duplicate') {
          report[outcome === 'delivered' ? 'delivered' : 'duplicates']++;
        } else if (outcome === 'drop') {
          report.dropped++;
        } else {
          item.tries += 1;
          item.nextAttemptAt = this.now() + this.backoff(item.tries);
          keep.push(item);
          report.retrying++;
        }
      }
      await this.save(keep);
      if (paused && !report.authError && (report.delivered || report.duplicates || keep.length === 0)) {
        await this.clearAuthError();
      }
      return report;
    }

    async sendOne(item) {
      try {
        const res = await this.client.postEvent(item.event);
        return res.body && res.body.duplicate ? 'duplicate' : 'delivered';
      } catch (err) {
        if (err && err.status === 422 && item.problem && !item.problemRegistered) {
          try {
            await this.client.createProblem(item.problem);
          } catch (regErr) {
            if (isAuthError(regErr)) return 'auth';
            if (!(regErr && regErr.status === 409)) {
              return regErr && regErr.status >= 400 && regErr.status < 500 ? 'drop' : 'retry';
            }
          }
          item.problemRegistered = true;
          return this.sendOne(item);
        }
        if (isAuthError(err)) return 'auth';
        if (err && err.status >= 400 && err.status < 500 && err.status !== 408 && err.status !== 429) {
          return 'drop';
        }
        return 'retry';
      }
    }
  }

  const api = { EventQueue, MemoryStorage, MAX_QUEUE };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.KTQueue = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
