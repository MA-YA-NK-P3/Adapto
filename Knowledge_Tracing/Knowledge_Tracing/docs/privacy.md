# Privacy

## What is stored

Interaction events (problem, result, attempt number, time spent, hint/solution flags,
optional self-reported confidence and blocker text), learner estimates and
recommendations. Learners are identified by an `externalRef` chosen by the client (the
extension generates a random `ext-<uuid>`, editable on its options page).

## What the extension sends

Nothing is sent while tracking is off (`trackingEnabled` defaults to `false`). With
tracking on, after LeetCode shows a submission verdict:

| step | fields |
|---|---|
| content script → background script (`chrome.runtime.sendMessage`, type `kt/submission`, stays in the browser) | `problem`: `problemId` (`LC<number>`), `number`, `title`, `url` (`https://leetcode.com/problems/<slug>/`), `staticDifficulty`, `concepts` (mapped from the page's topic tags); `result`, `timestamp`, `timeSpentSeconds`, `hintUsed`, `solutionViewed`, `sessionId` (random `sess-…` per page load) |
| background → `POST /api/events` | `userId`, `problemId`, `eventId`, `timestamp`, `attemptNumber` (counted locally per problem), `result`, `hintUsed`, `solutionViewed`, and when available `timeSpentSeconds`, `sessionId` |
| background → `POST /api/problems`, only if the backend answers 422 for an unknown problem and the difficulty was read | `problemKey`, `title`, `url`, `staticDifficulty`, `concepts` (`conceptCode`, `weight`) |
| popup → `GET /api/learners/{userId}/next-action` | the `userId` in the path |

Every request carries `X-API-Key` if one is configured. The extension never sends code,
the LeetCode username, page HTML, confidence, perceived difficulty or blocker text (the
API accepts the last three, but only other clients set them). Stored locally in
`chrome.storage.local`: settings (`apiBaseUrl`, `apiKey`, `userId`, `trackingEnabled`),
per-problem attempt counts (`kt.attempts`), up to 500 undelivered events (`kt.queue`),
the last delivery report (`kt.lastFlush`) and, while the backend rejects the API key, the
time of that rejection (`kt.authError`).

## Pseudonyms

`L-` + first 16 bytes (hex) of `HMAC-SHA256(salt, externalRef)` (`common/Pseudonyms.java`).
Used wherever learner data leaves the backend:

* research exports (`/api/research/*`),
* the Jev HTTP provider (`learner.id` in `kt-jev-v1`).

The salt is `KT_PSEUDONYM_SALT`. With a known salt anyone can re-identify a learner by
hashing guessed refs, so the research export **refuses to run** (HTTP 422) while the salt
is blank or the default `change-me`. Rotating the salt makes old and new exports unlinkable.

## Research export

Disabled by default (`KT_RESEARCH_EXPORT=false` → 404). When enabled it exports
pseudonymised events, the public catalog and rewarded decisions. Free-text fields
(blocker, feedback comments) are never exported. `sessionId` is exported as sent by the
client; clients should not put identifying data in it.

## External services

* `http` Jev provider: sends the pseudonym, ability estimates, the gap summary and public
  problem keys to `KT_JEV_HTTP_URL`.
* `scripts/jev_llm_server.py --llm` forwards that payload to the configured
  OpenAI-compatible endpoint (`LLM_BASE_URL`). Only use it with a provider whose data
  terms you accept; the default heuristic mode sends nothing anywhere.

## Synthetic data

Everything in `data/synthetic/` is generated; it contains no personal data.
