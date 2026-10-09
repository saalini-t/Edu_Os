"""PromptedProvider: implements the four provider operations on top of any JsonBackend. Output is validated against the
strict Pydantic schemas; invalid output raises ValidationError, which the agents treat as a provider failure."""
from __future__ import annotations

from app.llm import prompts
from app.llm.slim import SLIM, schema_for
from app.llm.backends import JsonBackend
from app.llm.schemas import (
    DoubtAnalysis, EvaluateRequest, EvaluationOut, ExplainRequest, Explanation, PracticeRequest, PracticeSet,
    UnderstandRequest,
)


MAX_TOKENS = {"understand": 300, "explain": 1200, "practice": 1600, "evaluate": 350}   # output caps: a looping model cannot run for minutes


class PromptedProvider:
    def __init__(self, backend: JsonBackend):
        self.backend = backend
        self.name, self.model = backend.name, backend.model

    def prompt_version(self, op: str) -> str:
        return prompts.VERSIONS[op]

    def understand(self, req: UnderstandRequest) -> DoubtAnalysis:
        system, user = prompts.understand(req)
        return SLIM["understand"].model_validate(self.backend.complete_json(system, user, schema_for("understand"), MAX_TOKENS["understand"])).to_internal()

    def explain(self, req: ExplainRequest) -> Explanation:
        system, user = prompts.explain(req)
        return SLIM["explain"].model_validate(self.backend.complete_json(system, user, schema_for("explain"), MAX_TOKENS["explain"])).to_internal()

    def generate_practice(self, req: PracticeRequest) -> PracticeSet:
        system, user = prompts.practice(req)
        return SLIM["practice"].model_validate(self.backend.complete_json(system, user, schema_for("practice"), MAX_TOKENS["practice"])).to_internal()

    def evaluate_answer(self, req: EvaluateRequest) -> EvaluationOut:
        system, user = prompts.evaluate(req)
        return SLIM["evaluate"].model_validate(self.backend.complete_json(system, user, schema_for("evaluate"), MAX_TOKENS["evaluate"])).to_internal()

    def warm(self) -> None:
        getattr(self.backend, "warm", lambda: None)()

    def status(self) -> dict:
        return {"provider": self.name, "model": self.model, "reachable": self.backend.ping()}
