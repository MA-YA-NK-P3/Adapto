"""kt-events-v1: the interaction-event interchange format (docs/data.md).

One JSON object per line (optionally gzip-compressed). The SAME schema is produced by the
synthetic simulator (scripts/generate_synthetic_dataset.py) and by the backend research
export (GET /api/research/events), so every training/evaluation script works on
either source.

Required fields
  schema            "kt-events-v1"
  learnerId         opaque learner id (pseudonymised in real exports)
  seq               0-based position in the learner's chronological sequence
  problemId         catalog key, e.g. "LC21"
  timestamp         ISO-8601 UTC
  attemptNumber     1-based attempt number on this problem
  result            ACCEPTED | WRONG_ANSWER | TIME_LIMIT_EXCEEDED | RUNTIME_ERROR | ABANDONED
  correct           1 if result == ACCEPTED else 0
Optional fields
  eventId, sessionId, timeSpentSeconds, hintUsed, solutionViewed
  truth             SYNTHETIC ONLY: {"pCorrect": .., "mastery": {concept: ..}}
"""
import gzip
import hashlib
import json
import pathlib
import uuid
from typing import Dict, Iterable, Iterator, List, Tuple

SCHEMA = "kt-events-v1"
RESULTS = ("ACCEPTED", "WRONG_ANSWER", "TIME_LIMIT_EXCEEDED", "RUNTIME_ERROR", "ABANDONED")
_NAMESPACE = uuid.UUID("6f1c2b1e-1d0a-4a57-9a53-4b2f0c1e7d10")


def event_id(learner_id: str, seq: int) -> str:
    """Deterministic event id, stable across regenerations with the same seed."""
    return str(uuid.uuid5(_NAMESPACE, f"{learner_id}:{seq}"))


def _open(path: pathlib.Path, mode: str):
    path = pathlib.Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding="utf-8")
    return open(path, mode.replace("t", ""), encoding="utf-8")


def write_jsonl(path: pathlib.Path, records: Iterable[dict]) -> int:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with _open(path, "wt") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
            n += 1
    return n


def read_jsonl(path: pathlib.Path) -> Iterator[dict]:
    with _open(pathlib.Path(path), "rt") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def validate(record: dict) -> None:
    for key in ("learnerId", "seq", "problemId", "timestamp", "attemptNumber", "result", "correct"):
        if key not in record:
            raise ValueError(f"missing field {key!r} in {record}")
    if record["result"] not in RESULTS:
        raise ValueError(f"unknown result {record['result']!r}")
    if int(record["correct"]) != (1 if record["result"] == "ACCEPTED" else 0):
        raise ValueError(f"correct/result mismatch in {record}")


def load_sequences(path: pathlib.Path, known_problems=None) -> Dict[str, List[dict]]:
    """Group events by learner, chronological (seq, then timestamp). Unknown problems dropped."""
    by_learner: Dict[str, List[dict]] = {}
    for r in read_jsonl(path):
        if known_problems is not None and r["problemId"] not in known_problems:
            continue
        by_learner.setdefault(r["learnerId"], []).append(r)
    for seq in by_learner.values():
        seq.sort(key=lambda e: (e.get("seq", 0), e["timestamp"]))
    return by_learner


def split_learners(learner_ids: Iterable[str], train: float = 0.70, val: float = 0.15,
                   salt: str = "kt-split-v1") -> Tuple[List[str], List[str], List[str]]:
    """Deterministic learner-level split by hash (no learner appears in two splits)."""
    tr, va, te = [], [], []
    for lid in sorted(learner_ids):
        h = int(hashlib.sha256(f"{salt}:{lid}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        (tr if h < train else va if h < train + val else te).append(lid)
    return tr, va, te
