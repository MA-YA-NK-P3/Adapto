"""Model status endpoints — port of recommendation/api/ModelStatusController.java
(docs/dkt.md, docs/jev.md). Operational introspection, always unauthenticated-gated
only by the API key filter like every other /api/** endpoint.

  GET /api/models/dkt   DKT component status (+ the loaded model's metadata when an
                        LSTM artifact is active)
  GET /api/models/jev   Jev decision-layer status, incl. the LinUCB arm statistics
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..decision import BanditFeatures, LinUCB
from ..dkt import LstmDktPredictor
from ..domain import ActionType
from .app import get_services, get_settings

router = APIRouter()


@router.get("/api/models/dkt")
def dkt_status(settings=Depends(get_settings), services=Depends(get_services)):
    cfg = settings.dkt
    predictor = services.dkt_predictor
    payload: Dict[str, Any] = {
        "enabled": cfg.enabled,
        "available": predictor.available(),
        "status": predictor.status() if cfg.enabled else "DISABLED",
        "modelPath": cfg.model_path,
        "blendWeight": cfg.blend_weight,
        "minHistory": cfg.min_history,
    }
    if isinstance(predictor, LstmDktPredictor):
        model = predictor.model
        payload.update({
            "concepts": list(model.concepts),
            "hiddenSize": model.hidden_size,
            "maxSequenceLength": model.max_sequence_length,
            "trainedOn": model.trained_on,
            "createdAt": model.created_at,
            "testAuc": model.test_auc,
        })
    return payload


@router.get("/api/models/jev")
def jev_status(settings=Depends(get_settings), services=Depends(get_services)):
    cfg = settings.jev
    stats = services.bandit_store.snapshot()
    prior = services.bandit_store.prior()
    arms = []
    for action in ActionType:  # Java: ActionType.values() declaration order
        arm_stats = stats.get(action.name, prior)
        arms.append({
            "arm": action.name,
            "updates": arm_stats.pulls,
            "meanReward": (arm_stats.reward_sum / arm_stats.pulls
                           if arm_stats.pulls > 0 else None),
            "theta": [float(v) for v in LinUCB.theta(arm_stats.a, arm_stats.b)],
        })
    return {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "providers": ["deterministic", "bandit", "http"],
        "confidenceThreshold": cfg.confidence_threshold,
        "bandit": {
            "alpha": cfg.bandit.alpha,
            "lambda": cfg.bandit.lambda_,
            "guarded": cfg.bandit.guarded,
            "features": list(BanditFeatures.NAMES),
            "arms": arms,
        },
        "http": {"url": cfg.http.url, "timeoutMs": cfg.http.timeout_ms},
    }
