"""PyTorch DKT (kt-dkt-v1) — LSTM knowledge tracing with a concept-level output head.

Architecture and feature contract: model/dkt/features.py (single source of truth).
Loss (DKT+, Yeung & Yeung 2018, adapted to multi-concept problems):
    L = BCE(next-step prediction)
      + lambda_r  * BCE(reconstruction of the CURRENT step from the post-update state)
      + lambda_w1 * mean |sigmoid(y_t) - sigmoid(y_{t-1})|          (waviness, L1)
      + lambda_w2 * mean (sigmoid(y_t) - sigmoid(y_{t-1}))^2        (waviness, L2)
      + lambda_item * mean(delta_q^2)                                (item offsets shrink to the prior)
"""
import datetime as _dt
import json
from typing import Dict, List

import torch
from torch import nn

from features import FORMAT


class DKT(nn.Module):
    def __init__(self, n_concepts: int, n_items: int, hidden: int = 64, dropout: float = 0.2):
        super().__init__()
        self.k = n_concepts
        self.hidden = hidden
        self.lstm = nn.LSTM(2 * n_concepts + 2, hidden, batch_first=True)
        self.drop = nn.Dropout(dropout)
        self.out = nn.Linear(hidden, n_concepts)
        self.item_offset = nn.Embedding(n_items, 1)
        nn.init.zeros_(self.item_offset.weight)
        self.gamma = nn.Parameter(torch.tensor(1.0))
        self.rho = nn.Parameter(torch.tensor(0.5))
        self.bias = nn.Parameter(torch.tensor(0.5))

    def concept_logits(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T, D] -> concept logits for the states h_0 .. h_T : [B, T+1, K]."""
        h, _ = self.lstm(x)
        h = torch.cat([torch.zeros_like(h[:, :1]), h], dim=1)
        return self.out(self.drop(h))

    def head(self, y, w, d, r, q):
        """Prediction logits. y: [B,T,K] concept logits, w: [B,T,K], d/r: [B,T], q: [B,T] item idx."""
        total = w.sum(-1).clamp_min(1e-6)
        concept = (w * y).sum(-1) / total
        return concept - self.gamma * d - self.item_offset(q).squeeze(-1) + self.rho * r + self.bias


def loss_fn(model: DKT, batch: Dict[str, torch.Tensor], lambda_r: float, lambda_w1: float,
            lambda_w2: float, lambda_item: float = 0.0):
    y_all = model.concept_logits(batch["x"])             # [B, T+1, K]
    mask = batch["mask"]
    bce = nn.functional.binary_cross_entropy_with_logits
    pred = model.head(y_all[:, :-1], batch["w"], batch["d"], batch["r"], batch["q"])
    n = mask.sum().clamp_min(1.0)
    main = (bce(pred, batch["y"], reduction="none") * mask).sum() / n
    total = main
    if lambda_r:
        recon = model.head(y_all[:, 1:], batch["w"], batch["d"], batch["r"], batch["q"])
        total = total + lambda_r * (bce(recon, batch["y"], reduction="none") * mask).sum() / n
    if lambda_w1 or lambda_w2:
        s = torch.sigmoid(y_all + model.bias)
        diff = (s[:, 1:] - s[:, :-1]) * mask.unsqueeze(-1)
        denom = n * model.k
        total = total + lambda_w1 * diff.abs().sum() / denom + lambda_w2 * (diff ** 2).sum() / denom
    if lambda_item:
        total = total + lambda_item * (model.item_offset.weight ** 2).mean()
    return total, main, torch.sigmoid(pred)


def _r(v, digits=7):
    if isinstance(v, list):
        return [_r(x, digits) for x in v]
    return float(f"{v:.{digits}g}")


def export(model: DKT, concepts: List[str], items: List[str], path, meta: dict) -> dict:
    """Write kt-dkt-v1 JSON (loaded by the backend and by features.ReferenceDkt)."""
    sd = {k: v.detach().cpu() for k, v in model.state_dict().items()}
    offsets = sd["item_offset.weight"].squeeze(-1).tolist()
    doc = {
        "format": FORMAT,
        "createdAt": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "concepts": list(concepts),
        "inputSize": 2 * len(concepts) + 2,
        "hiddenSize": model.hidden,
        **meta,
        "weights": {
            "lstm.weight_ih": _r(sd["lstm.weight_ih_l0"].tolist()),
            "lstm.weight_hh": _r(sd["lstm.weight_hh_l0"].tolist()),
            "lstm.bias_ih": _r(sd["lstm.bias_ih_l0"].tolist()),
            "lstm.bias_hh": _r(sd["lstm.bias_hh_l0"].tolist()),
            "out.weight": _r(sd["out.weight"].tolist()),
            "out.bias": _r(sd["out.bias"].tolist()),
            "gamma": _r(float(sd["gamma"])),
            "rho": _r(float(sd["rho"])),
            "bias": _r(float(sd["bias"])),
        },
        "itemOffsets": {pid: round(v, 5) for pid, v in zip(items, offsets) if abs(v) >= 1e-5},
    }
    with open(path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    return doc
