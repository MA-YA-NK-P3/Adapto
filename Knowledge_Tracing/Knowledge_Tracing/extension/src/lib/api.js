/* Minimal backend API client. `fetchImpl` is injectable for tests. */
(function (root) {
  'use strict';

  class ApiError extends Error {
    constructor(status, body) {
      super((body && body.message) || 'HTTP ' + status);
      this.status = status;
      this.body = body;
    }
  }

  class ApiClient {
    constructor({ baseUrl, apiKey, fetchImpl, timeoutMs = 8000 }) {
      this.baseUrl = String(baseUrl || 'http://localhost:8080').replace(/\/+$/, '');
      this.apiKey = apiKey || '';
      this.fetch = fetchImpl || root.fetch.bind(root);
      this.timeoutMs = timeoutMs;
    }

    async request(method, path, body) {
      const headers = { 'Content-Type': 'application/json' };
      if (this.apiKey) headers['X-API-Key'] = this.apiKey;
      const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => ctrl.abort(), this.timeoutMs) : null;
      try {
        const res = await this.fetch(this.baseUrl + path, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: ctrl ? ctrl.signal : undefined,
        });
        const text = await res.text();
        const json = text ? JSON.parse(text) : null;
        if (!res.ok) throw new ApiError(res.status, json);
        return { status: res.status, body: json };
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    postEvent(event) { return this.request('POST', '/api/events', event); }
    createProblem(problem) { return this.request('POST', '/api/problems', problem); }
    nextAction(userId) { return this.request('GET', '/api/learners/' + encodeURIComponent(userId) + '/next-action'); }
    state(userId) { return this.request('GET', '/api/learners/' + encodeURIComponent(userId) + '/state'); }
    health() { return this.request('GET', '/actuator/health'); }
  }

  const api = { ApiClient, ApiError };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.KTApi = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
