"""/api/models — port of recommendation.api.ModelStatusController: operational status
of the learned components (DKT, Jev bandit) for audits and demos. Key order matches
the Java LinkedHashMaps.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..decision import BanditFeatures, LinUCB
from ..dkt import LstmDktPredictor
from ..domain import ActionType
from .app import Services, get_services

router = APIRouter()


@router.get("/api/models/dkt")
def dkt_status(services: Services = Depends(get_services)) -> dict:
    cfg = services.settings.dkt
    predictor = services.dkt_predictor
    body = {
        "enabled": cfg.enabled,
        "available": predictor.available(),
        "status": predictor.status() if cfg.enabled else "DISABLED",
        "modelPath": cfg.model_path,
        "blendWeight": cfg.blend_weight,
        "minHistory": cfg.min_history,
    }
    if isinstance(predictor, LstmDktPredictor):
        model = predictor.model
        body.update({
            "concepts": list(model.concepts),
            "hiddenSize": model.hidden_size,
            "maxSequenceLength": model.max_sequence_length,
            "trainedOn": model.trained_on,
            "createdAt": model.created_at,
            "testAuc": model.test_auc,
        })
    return body


@router.get("/api/models/jev")
def jev_status(services: Services = Depends(get_services)) -> dict:
    cfg = services.settings.jev
    store = services.bandit_store
    stats = store.snapshot()
    arms = []
    for action in ActionType:  # definition order == ActionType.values() order
        s = stats.get(action.value, store.prior())
        arms.append({
            "arm": action.value,
            "updates": s.pulls,
            "meanReward": (s.reward_sum / s.pulls) if s.pulls > 0 else None,
            "theta": [float(v) for v in LinUCB.theta(s.a, s.b)],
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
