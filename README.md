# Adapto — AI-Powered Adaptive Learning & Focus Mode for LeetCode

An integrated technical interview preparation platform combining **distraction-free focus mode filtering** with **cognitive Knowledge Tracing AI**.

---

## Architecture Overview

```
                      ┌────────────────────────────────────────┐
                      │          Learner on LeetCode           │
                      └──────────────────┬─────────────────────┘
                                         │
                                         ▼
                      ┌────────────────────────────────────────┐
                      │       Adapto Chrome Extension          │
                      │  (Manifest V3 · popup + content + sw)  │
                      └───────┬────────────────────────▲───────┘
          1. Observation      │                        │ 3. Calibrated
             (verdict, time,  │                        │    Recommendation
             hints, solutions)│                        │    (problem + slug)
                              ▼                        │
                      ┌────────────────────────────────┴───────┐
                      │       KT_python Backend Engine         │
                      │       (FastAPI + SQLite :8081)         │
                      ├────────────────────────────────────────┤
                      │ • Rasch IRT Ability Estimation (θ)     │
                      │ • Beta-Bernoulli Concept Mastery       │
                      │ • Knowledge Graph (25 concepts/21 DAG) │
                      │ • Jev Decision Engine (Adaptive Loop)  │
                      └────────────────────────────────────────┘
```

### Components

1. **`adapto-extension/` (Unified Extension)**
   - **🧠 AI Tutor Mode:** Queries `KT_python` (`/api/learners/{userId}/next-action`), receives target problem recommendations, and dynamically locks LeetCode's UI to the recommended problem.
   - **📄 BYOS Mode (Bring Your Own Sheet):** Drag-and-drop `.txt` or `.csv` sheets to filter LeetCode exclusively to your custom syllabus.
   - **Observation Engine:** Intercepts LeetCode submissions, verdict (Accepted/WA/TLE), time spent, hint usage, and editorial visits, queuing and streaming them to the backend.

2. **`KT_python/` (The Cognitive Brain)**
   - Rebuilt on **FastAPI + SQLAlchemy + SQLite** (`http://127.0.0.1:8081`).
   - 1,000 seeded LeetCode problems, 25 foundational CS concepts, 21 prerequisite dependencies.
   - Rasch IRT ability scoring and Jev recommendation pipeline.
   - Includes full learner dashboard at `http://127.0.0.1:8081/dashboard.html`.

3. **`Knowledge_Tracing/` (Java Prototype & Research Base)**
   - Reference Spring Boot 3 implementation and research documentation.

---

## Quick Start Guide

### Step 1: Start the Backend (Port 8081)
Double-click `start_kt_backend.bat` or run:
```powershell
.\start_kt_backend.ps1
```
The server will start at **`http://127.0.0.1:8081`**.  
You can view the visual learner dashboard at **`http://127.0.0.1:8081/dashboard.html`**.

### Step 2: Load the Chrome Extension
1. Open Google Chrome and go to `chrome://extensions/`.
2. Turn ON **Developer mode** (top-right toggle).
3. Click **Load unpacked** (top-left button).
4. Select the folder: `d:\programs\FinalProject\adapto-extension`.

### Step 3: Use Adapto on LeetCode
1. Go to [LeetCode Problemset](https://leetcode.com/problemset/).
2. Click the **Adapto** icon in your Chrome toolbar:
   - **AI Tutor Tab:** Shows your current estimated ability ($\theta$), knowledge gap, and recommended next problem. Click **🎯 Filter LeetCode** to hide all distracting problems and keep only your recommended focus item!
   - **My Sheet Tab:** Upload your custom `.txt`/`.csv` sheet to filter to a personal syllabus.
3. Solve problems on LeetCode: Adapto automatically captures your submission verdicts and time, updating your cognitive profile in real-time.
