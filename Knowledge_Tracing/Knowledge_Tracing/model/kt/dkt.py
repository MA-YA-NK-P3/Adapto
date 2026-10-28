"""Deep Knowledge Tracing (DKT) — interface, dataset builder and honest availability check.

The trained LSTM lives in model/dkt/ (train_dkt.py, dkt_model.py) and runs in the backend
as LstmDktPredictor when KT_DKT_ENABLED=true; the shipped model is trained on SYNTHETIC
data only (docs/dkt.md). This stdlib-only module defines:
  * the sequence dataset format a DKT model would consume,
  * the minimum-data rule deciding whether training is meaningful,
  * an availability report that never pretends the model exists.
"""
import importlib.util
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

# Conservative minimums below which a recurrent model would not outperform IRT/BKT
# baselines reliably (order of magnitude of small public KT benchmarks).
MIN_LEARNERS = 1000
MIN_INTERACTIONS = 50_000


@dataclass
class DktDataset:
    skill_index: Dict[str, int]
    sequences: List[List[Tuple[int, int]]]  # per learner: (skill_id, correct)

    @property
    def interactions(self) -> int:
        return sum(len(s) for s in self.sequences)

    def one_hot_width(self) -> int:
        """Input width of the classic DKT encoding: 2 * number of skills."""
        return 2 * len(self.skill_index)


def build_dataset(sequences: Sequence[Sequence[Tuple[str, bool]]], tags: Dict[str, Dict[str, float]]) -> DktDataset:
    """Map (problem_id, correct) sequences to (primary skill id, correct) sequences."""
    skill_index: Dict[str, int] = {}
    out = []
    for seq in sequences:
        row = []
        for pid, correct in seq:
            primary = max(tags[pid].items(), key=lambda kv: kv[1])[0]
            sid = skill_index.setdefault(primary, len(skill_index))
            row.append((sid, int(bool(correct))))
        out.append(row)
    return DktDataset(skill_index, out)


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def availability(dataset: DktDataset, synthetic: bool) -> dict:
    reasons = []
    if not torch_available():
        reasons.append("PyTorch is not installed in this environment")
    if len(dataset.sequences) < MIN_LEARNERS:
        reasons.append(f"only {len(dataset.sequences)} learner sequences (< {MIN_LEARNERS})")
    if dataset.interactions < MIN_INTERACTIONS:
        reasons.append(f"only {dataset.interactions} interactions (< {MIN_INTERACTIONS})")
    if synthetic:
        reasons.append("data is SYNTHETIC; a DKT trained on it would only learn the simulator")
    return {
        "component": "DKT",
        "status": "UNAVAILABLE" if reasons else "TRAINABLE",
        "reasons": reasons,
        "learners": len(dataset.sequences),
        "interactions": dataset.interactions,
        "skills": len(dataset.skill_index),
    }
