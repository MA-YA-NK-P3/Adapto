"""Deep Knowledge Tracing (DKT) inference — the exported kt-dkt-v1 LSTM in-process.

Ports of dkt.DktModel (immutable model + forward pass), dkt.DktSnapshot (per-learner
readout), dkt.ProblemFeatures, dkt.DktPredictor / dkt.LstmDktPredictor /
dkt.UnavailableDktPredictor (the integration point and its two implementations) and
the startup wiring of dkt.DktConfiguration (build_predictor). The model contract is
specified in model/dkt/features.py and documented in docs/dkt.md:

    x_t   = [w*y, w*(1-y), (prior-5)/2, assisted]                     size 2K+2
    gates = W_ih x + b_ih + W_hh h + b_hh      (PyTorch gate order i, f, g, o)
    c'    = f*c + i*g,  h' = o*tanh(c'),  y = W_out h + b_out         size K
    p(q)  = sigmoid(sum_k w_qk y_k / sum_k w_qk - gamma*d_q - delta_q + rho*r_q + bias)

The forward pass runs on float64 numpy arrays. tests/test_dkt.py pins parity with
the Python reference (and thereby the Java DktModel) to 1e-9 against
backend/src/test/resources/dkt/fixture-*.json — the fixture DktModelTest uses.
"""
from __future__ import annotations

import json
import logging
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Union

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import InteractionEvent, Problem, ProblemConcept

logger = logging.getLogger(__name__)


def sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid — the branch structure of DktModel.sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _sigmoid_vec(x: np.ndarray) -> np.ndarray:
    """Elementwise sigmoid with the same branches as the scalar version."""
    out = np.empty_like(x)
    pos = x >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    neg = ~pos
    z = np.exp(x[neg])
    out[neg] = z / (1.0 + z)
    return out


# --- JSON parsing helpers (Jackson JsonNode semantics) -----------------------------


def _text(node, default: str) -> str:
    """JsonNode.asText(default): only textual nodes keep their value."""
    return node if isinstance(node, str) else default


def _double(node) -> float:
    """JsonNode.asDouble(): non-numeric nodes are 0.0."""
    return float(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else 0.0


def _int(node, default: int) -> int:
    """JsonNode.asInt(default)."""
    return int(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else default


def _doubles(node) -> Dict[str, float]:
    """DktModel.doubles(JsonNode): an object of numbers, empty when absent."""
    if not isinstance(node, dict):
        return {}
    return {str(k): float(v) for k, v in node.items()}


def _matrix(node, rows: int, cols: int, name: str) -> List[List[float]]:
    if not isinstance(node, list) or len(node) != rows:
        got = len(node) if isinstance(node, list) else 0
        raise ValueError(f"{name}: expected {rows} rows, got {got}")
    return [_vector(row, cols, f"{name}[{r}]") for r, row in enumerate(node)]


def _vector(node, size: int, name: str) -> List[float]:
    if not isinstance(node, list) or len(node) != size:
        got = len(node) if isinstance(node, list) else 0
        raise ValueError(f"{name}: expected length {size}, got {got}")
    return [float(v) for v in node]


class DktModel:
    """Immutable kt-dkt-v1 LSTM (port of dkt.DktModel; docs/dkt.md).

    Loads the JSON exported by model/dkt/dkt_model.py and runs the forward pass.
    IllegalArgumentException on the Java side maps to ValueError here, with the same
    messages ("unsupported DKT model format: ...", "<weight>: expected N rows, got M").
    """

    FORMAT = "kt-dkt-v1"
    DIFFICULTY_CENTER = 5.0
    DIFFICULTY_SCALE = 2.0

    class State:
        """LSTM hidden and cell state (the DktModel.State record)."""

        __slots__ = ("h", "c")

        def __init__(self, h: np.ndarray, c: np.ndarray):
            self.h = h
            self.c = c

    def __init__(self, doc: Dict):
        fmt = doc.get("format")
        if fmt != self.FORMAT:
            raise ValueError("unsupported DKT model format: " + _text(fmt, ""))
        self._concepts = [str(c) for c in (doc.get("concepts") or [])]
        self._concept_index = {code: i for i, code in enumerate(self._concepts)}
        self._hidden = _int(doc.get("hiddenSize"), 0)
        k = len(self._concepts)
        self._input_size = 2 * k + 2
        w = doc.get("weights")
        w = w if isinstance(w, dict) else {}
        self._w_ih = np.array(
            _matrix(w.get("lstm.weight_ih"), 4 * self._hidden, self._input_size, "lstm.weight_ih"),
            dtype=np.float64)
        self._w_hh = np.array(
            _matrix(w.get("lstm.weight_hh"), 4 * self._hidden, self._hidden, "lstm.weight_hh"),
            dtype=np.float64)
        b_ih = _vector(w.get("lstm.bias_ih"), 4 * self._hidden, "lstm.bias_ih")
        b_hh = _vector(w.get("lstm.bias_hh"), 4 * self._hidden, "lstm.bias_hh")
        self._bias4 = np.array([a + b for a, b in zip(b_ih, b_hh)], dtype=np.float64)
        self._w_out = np.array(
            _matrix(w.get("out.weight"), k, self._hidden, "out.weight"), dtype=np.float64)
        self._b_out = np.array(_vector(w.get("out.bias"), k, "out.bias"), dtype=np.float64)
        self._gamma = _double(w.get("gamma"))
        self._rho = _double(w.get("rho"))
        self._bias = _double(w.get("bias"))
        self._item_offsets = _doubles(doc.get("itemOffsets"))
        self._concept_baseline = _doubles(doc.get("conceptBaseline"))
        self._max_sequence_length = _int(doc.get("maxSequenceLength"), 200)
        self._trained_on = _text(doc.get("trainedOn"), "unknown")
        self._created_at = _text(doc.get("createdAt"), "")
        self._test_auc: Optional[float] = None
        metrics = doc.get("metrics")
        if isinstance(metrics, dict):
            test = metrics.get("test")
            if isinstance(test, dict):
                auc = test.get("auc")
                if isinstance(auc, (int, float)) and not isinstance(auc, bool):
                    self._test_auc = float(auc)

    @classmethod
    def load(cls, path: Union[str, os.PathLike]) -> "DktModel":
        """Read and parse an exported kt-dkt-v1 JSON model file (DktModel.load)."""
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))

    @classmethod
    def from_json(cls, doc: Dict) -> "DktModel":
        """Parse an already-decoded model document (DktModel.fromJson)."""
        return cls(doc)

    @staticmethod
    def scaled_difficulty(prior_difficulty: float) -> float:
        """(prior - 5) / 2 — the difficulty scale the model was trained on."""
        return ((prior_difficulty - DktModel.DIFFICULTY_CENTER)
                / DktModel.DIFFICULTY_SCALE)

    def initial_state(self) -> "DktModel.State":
        return DktModel.State(np.zeros(self._hidden, dtype=np.float64),
                              np.zeros(self._hidden, dtype=np.float64))

    def input(self, weights, correct: bool, scaled_difficulty: float,
              assisted: bool) -> np.ndarray:
        """Input vector of one observed interaction (weights aligned with concepts)."""
        k = len(self._concepts)
        x = np.zeros(self._input_size, dtype=np.float64)
        w = np.asarray(weights, dtype=np.float64)
        if correct:
            x[0:k] = w
        else:
            x[k:2 * k] = w
        x[2 * k] = scaled_difficulty
        x[2 * k + 1] = 1.0 if assisted else 0.0
        return x

    def step(self, state: "DktModel.State", x) -> "DktModel.State":
        """One LSTM recurrence step (PyTorch gate order i, f, g, o)."""
        x = np.asarray(x, dtype=np.float64)
        h = self._hidden
        # np.matmul via the macOS Accelerate BLAS reports spurious FP flags
        # (divide-by-zero / overflow / invalid) from the beta=0 GEMM epilogue; the
        # results are unaffected — the 1e-9 fixture parity test pins them. Every
        # operand is provably bounded (h comes out of tanh), so no genuine
        # non-finite intermediate can occur here.
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            gates = self._bias4 + self._w_ih @ x + self._w_hh @ state.h
        i = _sigmoid_vec(gates[0:h])
        f = _sigmoid_vec(gates[h:2 * h])
        g = np.tanh(gates[2 * h:3 * h])
        o = _sigmoid_vec(gates[3 * h:4 * h])
        c = f * state.c + i * g
        out = o * np.tanh(c)
        return DktModel.State(out, c)

    def concept_logits(self, state: "DktModel.State") -> np.ndarray:
        """Concept head readout y = W_out h + b_out, in model concept order."""
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):  # see step()
            return self._b_out + self._w_out @ state.h

    def predict(self, concept_logits, weights, scaled_difficulty: float,
                attempted: bool, problem_key: Optional[str]) -> float:
        """P(correct) of the next attempt on a problem, given precomputed concept logits."""
        w = np.asarray(weights, dtype=np.float64)
        total = float(np.sum(w))
        dot = float(w @ np.asarray(concept_logits, dtype=np.float64))
        concept = dot / total if total > 0.0 else 0.0
        z = (concept - self._gamma * scaled_difficulty
             - self._item_offsets.get(problem_key, 0.0)
             + (self._rho if attempted else 0.0) + self._bias)
        return sigmoid(z)

    def mastery(self, concept_logits, k: int) -> float:
        """Concept mastery readout: first attempt on an average-difficulty problem."""
        return sigmoid(float(concept_logits[k]) + self._bias)

    def relative_strength(self, concept_logits, k: int) -> float:
        """Logit minus the training-population mean: below 0 = weaker than the average learner."""
        return float(concept_logits[k]) - self._concept_baseline.get(self._concepts[k], 0.0)

    @property
    def concepts(self) -> List[str]:
        """Concept codes in model order; treat the returned list as immutable."""
        return self._concepts

    def concept_index(self, code: str) -> Optional[int]:
        return self._concept_index.get(code)

    @property
    def hidden_size(self) -> int:
        return self._hidden

    @property
    def max_sequence_length(self) -> int:
        return self._max_sequence_length

    @property
    def trained_on(self) -> str:
        return self._trained_on

    @property
    def created_at(self) -> str:
        return self._created_at

    @property
    def test_auc(self) -> Optional[float]:
        return self._test_auc


@dataclass(frozen=True)
class ProblemFeatures:
    """DKT view of a problem (port of the dkt.ProblemFeatures record): concept weights
    aligned with the model's concept order and the scaled prior difficulty (the same
    quantity the model was trained on)."""
    problem_key: str
    weights: np.ndarray
    scaled_difficulty: float

    def has_known_concept(self) -> bool:
        return bool(np.any(self.weights > 0.0))


class DktSnapshot:
    """DKT state of one learner after replaying their most recent events (port of
    dkt.DktSnapshot). Concept logits are computed once, so scoring every problem in
    the catalog is a K-length dot product each."""

    def __init__(self, model: DktModel, state: "DktModel.State", attempted: Set[uuid.UUID],
                 features: Callable[[uuid.UUID], Optional[ProblemFeatures]],
                 events_replayed: int):
        self._model = model
        self._concept_logits = model.concept_logits(state)
        self._attempted = attempted
        self._features = features
        self.events_replayed = events_replayed

    def predict_success(self, problem: Problem) -> Optional[float]:
        """None when the problem has no concept the model knows (no DKT evidence to use)."""
        f = self._features(problem.id)
        if f is None or not f.has_known_concept():
            return None
        return self._model.predict(self._concept_logits, f.weights, f.scaled_difficulty,
                                   problem.id in self._attempted, problem.problem_key)

    def mastery(self) -> Dict[str, float]:
        """Concept code -> DKT mastery estimate, in model concept order."""
        return {code: self._model.mastery(self._concept_logits, k)
                for k, code in enumerate(self._model.concepts)}


class DktPredictor:
    """Deep Knowledge Tracing integration point (port of the dkt.DktPredictor
    interface; docs/dkt.md).

    LstmDktPredictor runs an exported kt-dkt-v1 model in-process when
    settings.dkt.enabled and the model file loads; otherwise UnavailableDktPredictor
    is active and the system runs on IRT + Beta mastery.
    """

    def snapshot(self, learner_id: uuid.UUID) -> Optional[DktSnapshot]:
        """Learner state after replaying their recent events, if a model is available."""
        raise NotImplementedError

    def status(self) -> str:
        """Human-readable status for the response's model report."""
        raise NotImplementedError

    def available(self) -> bool:
        return False


class LstmDktPredictor(DktPredictor):
    """In-process DKT inference (port of dkt.LstmDktPredictor).

    Per request it replays the learner's most recent max_sequence_length events from
    h_0 = 0 — the same windowing the model was trained with (model/dkt/data.py) — so
    no hidden state has to be persisted.

    Problem features are cached: concept tags of an existing problem never change
    through the API, and newly registered problems are loaded on first use. The Java
    double-checked locking around the bulk load is unnecessary under the GIL and the
    single-session-per-request wiring, so _ensure_loaded is a plain flag check.
    """

    def __init__(self, session: Session, settings: Settings, model: DktModel, source: str = ""):
        self.session = session
        self.settings = settings
        self.model = model
        self._source = source
        self._features: Dict[uuid.UUID, ProblemFeatures] = {}
        self._bulk_loaded = False

    def snapshot(self, learner_id: uuid.UUID) -> Optional[DktSnapshot]:
        self._ensure_loaded()
        recent = list(self.session.scalars(
            select(InteractionEvent)
            .where(InteractionEvent.learner_id == learner_id)
            .order_by(InteractionEvent.occurred_at.desc())
            .limit(self.model.max_sequence_length)))
        recent.reverse()
        state = self.model.initial_state()
        replayed = 0
        for e in recent:
            f = self._features_of(e.problem_id)
            if f is None:
                continue
            state = self.model.step(state, self.model.input(
                f.weights, e.result.is_correct, f.scaled_difficulty,
                e.hint_used or e.solution_viewed))
            replayed += 1
        attempted = set(self.session.scalars(
            select(InteractionEvent.problem_id)
            .where(InteractionEvent.learner_id == learner_id)
            .distinct()))
        return DktSnapshot(self.model, state, attempted, self._features_of, replayed)

    def status(self) -> str:
        auc = f", test AUC {self.model.test_auc:.3f}" if self.model.test_auc is not None else ""
        return (f"ENABLED (LSTM {DktModel.FORMAT}, K={len(self.model.concepts)}, "
                f"H={self.model.hidden_size}, trained on {self.model.trained_on} data "
                f"{self.model.created_at}{auc}; {self._source})")

    def available(self) -> bool:
        return True

    def _features_of(self, problem_id: uuid.UUID) -> Optional[ProblemFeatures]:
        cached = self._features.get(problem_id)
        if cached is not None:
            return cached
        problem = self.session.get(Problem, problem_id)
        if problem is None:
            return None  # not cached — a later registration must be picked up
        links = list(self.session.scalars(
            select(ProblemConcept).where(ProblemConcept.problem_id == problem_id)))
        features = self._build(problem, links)
        self._features[problem_id] = features
        return features

    def _ensure_loaded(self) -> None:
        if self._bulk_loaded:
            return
        tags: Dict[uuid.UUID, List[ProblemConcept]] = {}
        for pc in self.session.scalars(select(ProblemConcept)):
            tags.setdefault(pc.problem_id, []).append(pc)
        for problem in self.session.scalars(select(Problem)):
            self._features[problem.id] = self._build(problem, tags.get(problem.id, []))
        self._bulk_loaded = True
        logger.info("DKT feature cache loaded for %d problems", len(self._features))

    def _build(self, problem: Problem, links: List[ProblemConcept]) -> ProblemFeatures:
        w = np.zeros(len(self.model.concepts), dtype=np.float64)
        for pc in links:
            k = self.model.concept_index(pc.concept.code)
            if k is not None:
                w[k] = pc.weight
        return ProblemFeatures(problem.problem_key, w,
                               DktModel.scaled_difficulty(problem.prior_difficulty))


class UnavailableDktPredictor(DktPredictor):
    """Port of dkt.UnavailableDktPredictor: empty snapshots, reason in the status."""

    def __init__(self, reason: str):
        self._reason = reason

    def snapshot(self, learner_id: uuid.UUID) -> Optional[DktSnapshot]:
        return None

    def status(self) -> str:
        return "UNAVAILABLE (" + self._reason + ")"


def build_predictor(session: Session, settings: Settings) -> DktPredictor:
    """Choose the DKT implementation (port of DktConfiguration.dktPredictor).

    A missing or invalid model file never prevents startup: the system degrades to
    IRT + Beta mastery and reports why in the next-action response (models.dkt).
    When DKT is disabled the model file is never touched.
    """
    if not settings.dkt.enabled:
        return UnavailableDktPredictor("disabled by knowledge-tracer.dkt.enabled=false")
    path = Path(os.path.abspath(settings.dkt.model_path))
    if not os.access(path, os.R_OK):
        logger.warning("DKT enabled but model file not readable: %s", path)
        return UnavailableDktPredictor(
            f"model file not found: {path}"
            " — train with model/dkt/train_dkt.py or the Colab notebook")
    try:
        model = DktModel.load(path)
        logger.info("DKT model loaded from %s (K=%d, H=%d, trainedOn=%s)",
                    path, len(model.concepts), model.hidden_size, model.trained_on)
        return LstmDktPredictor(session, settings, model, source=path.name)
    except Exception as exc:
        logger.error("DKT model at %s could not be loaded: %s", path, exc)
        return UnavailableDktPredictor(f"invalid model file {path.name}: {exc}")
