"""kt-events-v1 sequences -> DKT training windows (standard library only)."""
from typing import Dict, List, Sequence

from features import Encoder, assisted


def encode_sequence(encoder: Encoder, events: Sequence[dict], item_index: Dict[str, int]) -> Dict[str, list]:
    """Per-step arrays for one learner, chronological.

    x[t]  input vector of event t (fed AFTER predicting it)
    w[t], d[t], r[t], q[t], y[t]  target features/label of event t (q = item index)
    r[t] is 1 if the learner attempted the same problem earlier in the FULL history.
    """
    seen = set()
    out = {"x": [], "w": [], "d": [], "r": [], "q": [], "y": []}
    for e in events:
        pid = e["problemId"]
        y = int(e["correct"])
        out["q"].append(item_index[pid])
        out["w"].append(encoder.weights[pid])
        out["d"].append(encoder.difficulty[pid])
        out["r"].append(1.0 if pid in seen else 0.0)
        out["y"].append(float(y))
        out["x"].append(encoder.input_vector(pid, y, assisted(e)))
        seen.add(pid)
    return out


def windows(seq: Dict[str, list], max_len: int) -> List[Dict[str, list]]:
    """Split into consecutive non-overlapping windows; each window restarts at h_0 = 0.

    The backend replays at most `max_len` recent events, so training and inference agree.
    """
    n = len(seq["y"])
    return [{k: v[s:s + max_len] for k, v in seq.items()} for s in range(0, n, max_len)]


def last_window(seq: Dict[str, list], max_len: int) -> Dict[str, list]:
    return {k: v[-max_len:] for k, v in seq.items()}
