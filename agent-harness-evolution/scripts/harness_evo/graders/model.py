"""Model-based graders (spec section 7, step 4): budgeted, cached, and calibration-gated.

Each grader is a YAML file in evals/graders/model/. A grader's findings can
influence the verdict only once evals/graders/model/calibration.json records at
least 30 human-labelled runs with 80% or better agreement for its current version.
Results are cached by content hash. Calls use the Anthropic SDK (optional extra
`harness-evo[model]`) with structured JSON output and server-side refusal fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .. import pricing
from ..paths import skill_file

CALIBRATION_MIN_RUNS = 30
CALIBRATION_MIN_AGREEMENT = 0.80
DEFAULT_MODEL = "claude-opus-5-5"
SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "flag"]},
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "explanation": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "severity", "explanation", "evidence"],
    "additionalProperties": False,
}


def grader_dir() -> Path:
    return skill_file("evals", "graders", "model")


def load_graders(directory: Path | None = None) -> list[dict]:
    from .._yaml import yaml
    directory = directory or grader_dir()
    out = []
    for p in sorted(directory.glob("*.yaml")):
        g = yaml.safe_load(p.read_text()) or {}
        if g.get("id"):
            out.append(g)
    return out


def calibration(directory: Path | None = None) -> dict:
    try:
        return json.loads(((directory or grader_dir()) / "calibration.json").read_text())
    except (OSError, ValueError):
        return {}


def calibration_status(g: dict, cal: dict) -> dict:
    rec = cal.get(g["id"]) or {}
    ok = (str(rec.get("version")) == str(g.get("version")) and rec.get("runs", 0) >= CALIBRATION_MIN_RUNS
          and rec.get("agreement", 0) >= CALIBRATION_MIN_AGREEMENT)
    return {"calibrated": ok, "runs": rec.get("runs", 0), "agreement": rec.get("agreement"),
            "calibrated_version": rec.get("version")}


def build_prompt(g: dict, inputs: dict) -> tuple[str, str]:
    system = (f"You are a code-review grader. {g['question'].strip()}\n\nRubric:\n{g['rubric'].strip()}\n\n"
              "Judge only from the material provided. Flag only when the rubric is clearly met, and cite "
              "file paths, diff lines or trace references as evidence.")
    parts = [f"<{k}>\n{v}\n</{k}>" for k, v in inputs.items() if v]
    return system, "\n\n".join(parts)


def _estimate(model: str, system: str, user: str, max_tokens: int) -> float:
    tokens = int((len(system) + len(user)) / 3.5) + 50
    return pricing.cost(model, {"input_tokens": tokens, "output_tokens": max_tokens}) or 0.0


PROVIDERS = ("anthropic", "bedrock", "vertex")


def provider_for(graders: list[dict]) -> str:
    """HARNESS_EVO_MODEL_PROVIDER, else a grader's `provider`, else the first-party API."""
    p = os.environ.get("HARNESS_EVO_MODEL_PROVIDER") or next((g["provider"] for g in graders if g.get("provider")), "")
    return p if p in PROVIDERS else "anthropic"


def make_client(provider: str):
    """An SDK client for the provider. ANTHROPIC_BASE_URL (gateways) is honoured by the first-party client."""
    import anthropic
    if provider == "bedrock":
        return anthropic.AnthropicBedrockMantle(aws_region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    if provider == "vertex":
        return anthropic.AnthropicVertex(project_id=os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"),
                                         region=os.environ.get("CLOUD_ML_REGION", "global"))
    return anthropic.Anthropic()


def _call(client, g: dict, system: str, user: str, provider: str = "anthropic") -> tuple[dict | None, dict, str | None]:
    model = g.get("model", DEFAULT_MODEL)
    common = {"max_tokens": int(g.get("max_tokens", 4000)), "system": system,
              "output_config": {"effort": g.get("effort", "medium"),
                                "format": {"type": "json_schema", "schema": SCHEMA}},
              "messages": [{"role": "user", "content": user}]}
    if provider == "anthropic":
        # Server-side refusal fallback exists only on the first-party API.
        resp = client.beta.messages.create(model=model, betas=["server-side-fallback-2026-07-01"],
                                           fallbacks="default", **common)
    else:
        if provider == "bedrock" and not model.startswith("anthropic."):
            model = "anthropic." + model
        resp = client.messages.create(model=model, **common)
    usage = {k: getattr(resp.usage, k, 0) or 0 for k in
             ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    if resp.stop_reason == "refusal":
        return None, usage, "refused"
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        return None, usage, f"no text output (stop_reason {resp.stop_reason})"
    return json.loads(text), usage, None


def run(inputs: dict, budget_usd: float, cache_dir: Path, client=None, graders: list[dict] | None = None,
        cal: dict | None = None) -> dict:
    graders = load_graders() if graders is None else graders
    cal = calibration() if cal is None else cal
    provider = provider_for(graders)
    if client is None:
        try:
            client = make_client(provider)
        except ImportError:
            extra = "" if provider == "anthropic" else f" with the {provider} extra (pip install 'anthropic[{provider}]')"
            return {"skipped": f"the anthropic SDK is not installed (pip install 'harness-evo[model]'){extra}",
                    "results": []}
    cache_dir.mkdir(parents=True, exist_ok=True)
    spent, results = 0.0, []
    for g in graders:
        model = g.get("model", DEFAULT_MODEL)
        system, user = build_prompt(g, inputs)
        status = calibration_status(g, cal)
        base = {"grader": g["id"], "version": str(g.get("version")), "model": model, **status}
        key = hashlib.sha256(json.dumps([g["id"], g.get("version"), provider, model, system, user]).encode()).hexdigest()
        cached = cache_dir / f"{key}.json"
        if cached.exists():
            results.append({**json.loads(cached.read_text()), **status, "cached": True})
            continue
        estimate = _estimate(model, system, user, int(g.get("max_tokens", 4000)))
        if spent + estimate > budget_usd:
            results.append({**base, "skipped": f"over the per-PR budget (estimate ${estimate:.3f})"})
            continue
        try:
            data, usage, problem = _call(client, g, system, user, provider)
        except Exception as exc:  # a grader failure never fails the PR check
            results.append({**base, "skipped": f"call failed: {type(exc).__name__}: {exc}"})
            continue
        cost = pricing.cost(model, usage) or estimate
        spent += cost
        rec = {**base, "cost_usd": cost, "usage": usage}
        if problem:
            rec["skipped"] = problem
        else:
            rec["output"] = data
            cached.write_text(json.dumps(rec))
        results.append(rec)
    return {"spent_usd": round(spent, 4), "budget_usd": budget_usd, "results": results}
