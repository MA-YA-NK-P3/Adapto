"""Problem pool + prerequisite graph loaded from data/seed/catalog.json."""
import json
import pathlib
from collections import deque
from typing import Dict, List

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "data" / "seed" / "catalog.json"


class Catalog:
    def __init__(self, data: dict):
        self.concepts: List[str] = [c["code"] for c in data["concepts"]]
        self.prerequisites: Dict[str, List[str]] = {c["code"]: list(c["prerequisites"]) for c in data["concepts"]}
        self.problems: List[dict] = data["problems"]
        self.labels: Dict[str, str] = {p["problemId"]: p["staticDifficulty"] for p in self.problems}
        self.tags: Dict[str, Dict[str, float]] = {}
        for p in self.problems:
            self.tags[p["problemId"]] = {c: float(w) for c, w in p["conceptWeights"].items()
                                         if c in self.prerequisites}

    @classmethod
    def load(cls, path: pathlib.Path = DEFAULT_CATALOG) -> "Catalog":
        return cls(json.loads(pathlib.Path(path).read_text()))

    def all_prerequisites(self, code: str) -> List[str]:
        """Transitive prerequisites, breadth-first, cycle-safe."""
        seen, out, queue = {code}, [], deque([code])
        while queue:
            for p in self.prerequisites.get(queue.popleft(), []):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
                    queue.append(p)
        return out

    def problems_for(self, code: str) -> List[str]:
        return [pid for pid, tags in self.tags.items() if code in tags]
