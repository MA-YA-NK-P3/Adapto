"""Knowledge Tracer — Python replica of the Spring Boot backend (backend/) plus the
DKT inference model (model/artifacts/dkt/dkt-model.json).

The package mirrors the Java architecture module by module so the two can be read
side by side. Each module docstring names the Java class it ports.

Java package                     Python module
-----------------------------    --------------------------
config.KnowledgeTracerProperties config.py
common.exception.*               errors.py
(common.Pseudonyms)              pseudonyms.py
learner/problem/concept/...      domain.py  (all JPA entities)
irt.IrtModel / IrtService        irt.py
mastery.ConceptMasteryService    mastery.py
localization.*                   localization.py
state.LearnerStateService        state.py
event.EventIngestionService      ingestion.py
concept.KnowledgeGraphService    knowledge_graph.py
gap.KnowledgeGapService          knowledge_gap.py
recommendation.CandidateGenerationService / SuccessEstimator
                                 candidates.py / success.py
decision.* (router, providers, validator, fallback, bandit, rewards)
                                 decision.py
recommendation.RecommendationService / OutcomeRewardService
                                 recommendation.py / decision.py
dkt.DktModel / LstmDktPredictor  dkt.py
research.ResearchExportService   research.py
*.api.* controllers              api.py     (FastAPI)
Service wiring (@Configuration)  container.py
"""
