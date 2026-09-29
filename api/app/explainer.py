"""Explanations and summaries (design section 12).

The LLM only ever writes prose. Nothing it returns is parsed into an action.
Every call has a 5 s timeout and sits behind a circuit breaker, and every use
has a deterministic template to fall back on, so the platform works the same
with no API key at all.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from prometheus_client import Counter

from app.config import settings

log = structlog.get_logger()
LLM_FALLBACKS = Counter("fsa_llm_fallback_total", "Times a template was used instead of the LLM", ["reason"])

SYSTEM = (
    "You explain decisions made by a fuel supply planning system to operations staff. "
    "All data is from a simulation. Write plain, specific language, no more than three sentences, "
    "no bullet points and no markdown. Use only the numbers in the record; never invent figures."
)


class CircuitBreaker:
    """Opens after 3 failures in a row, then lets one trial call through every 30 s."""

    def __init__(self, threshold: int = 3, cooldown_s: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - self.opened_at >= self.cooldown_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


class Explainer:
    def __init__(self) -> None:
        self.breaker = CircuitBreaker()
        self._cache: dict[str, str] = {}
        self._client: Any = None
        if settings.llm_enabled:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=settings.llm_api_key, timeout=5.0, max_retries=0)

    def status(self) -> str:
        if self._client is None:
            return "fallback"   # no key configured: templates only
        return {"closed": "healthy", "half_open": "degraded", "open": "fallback"}[self.breaker.state]

    async def _ask(self, prompt: str, max_tokens: int = 400) -> str | None:
        if self._client is None:
            LLM_FALLBACKS.labels("disabled").inc()
            return None
        if not self.breaker.allow():
            LLM_FALLBACKS.labels("breaker_open").inc()
            return None
        import anthropic

        try:
            response = await self._client.messages.create(
                model=settings.llm_model,
                max_tokens=max_tokens,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low"},
                # route around a safety-classifier refusal instead of failing the request
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            )
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            self.breaker.failure()
            LLM_FALLBACKS.labels("unreachable").inc()
            log.warning("llm.unreachable", error=str(exc)[:200])
            return None
        except anthropic.RateLimitError:
            self.breaker.failure()
            LLM_FALLBACKS.labels("rate_limited").inc()
            return None
        except anthropic.APIStatusError as exc:
            self.breaker.failure()
            LLM_FALLBACKS.labels(f"http_{exc.status_code}").inc()
            log.warning("llm.error", status=exc.status_code, error=str(exc.message)[:200])
            return None

        if response.stop_reason == "refusal":
            self.breaker.success()
            LLM_FALLBACKS.labels("refusal").inc()
            return None
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        self.breaker.success()
        if not text:
            LLM_FALLBACKS.labels("empty").inc()
            return None
        return text

    async def explain_decision(self, record: dict[str, Any]) -> tuple[str, str]:
        """Returns (text, source) where source is "llm" or "template"."""
        decision_id = record.get("decision_id", "")
        if decision_id in self._cache:
            return self._cache[decision_id], "llm"
        template = record.get("explanation") or "No explanation available."
        compact = {k: record.get(k) for k in ("station", "fuel", "mode", "signals", "constraints_binding",
                                              "action", "alternatives", "impact", "confidence", "gate", "outcome")}
        prompt = ("Explain this decision to an operator in at most three sentences: what is at risk, what the "
                  "system did or recommends, and what it changes. Mention the constraint that limited it, if any.\n\n"
                  + json.dumps(compact, default=str))
        text = await self._ask(prompt)
        if text is None:
            return template, "template"
        self._cache[decision_id] = text
        if len(self._cache) > 2000:
            self._cache.pop(next(iter(self._cache)))
        return text, "llm"

    async def summarize(self, kind: str, context: dict[str, Any], template: str) -> tuple[str, str]:
        prompts = {
            "incident": "Summarise this incident for an operations handover in at most four sentences.",
            "state": "Summarise the current state of the fuel network in at most four sentences, "
                     "leading with anything at risk.",
        }
        text = await self._ask(prompts[kind] + "\n\n" + json.dumps(context, default=str))
        return (text, "llm") if text else (template, "template")

    async def answer(self, question: str, context: dict[str, Any], template: str) -> tuple[str, str]:
        prompt = ("An operator asks a question about the simulated fuel network. Answer from the context only; "
                  "if the context doesn't contain the answer, say so. You can't take actions.\n\n"
                  f"Question: {question}\n\nContext:\n{json.dumps(context, default=str)}")
        text = await self._ask(prompt, max_tokens=600)
        return (text, "llm") if text else (template, "template")


def state_template(state: dict[str, Any]) -> str:
    status = state.get("status", {})
    metrics = state.get("metrics") or {}
    risky = []
    for station in state.get("stations", []):
        for fuel, info in station["fuels"].items():
            p = info["no_action"]["p_stockout"]
            if p >= 0.2:
                risky.append(f"{station['name']} {fuel.lower()} ({p:.0%})")
    parts = [f"Tick {state.get('tick')}, mode {status.get('mode')}, autonomy {status.get('autonomy')}."]
    if metrics:
        parts.append(f"Service level so far is {metrics.get('service_level', 1):.2%}.")
    parts.append("Stations at risk without action: " + (", ".join(risky[:6]) if risky else "none") + ".")
    alerts = [a for a in state.get("alerts", []) if a.get("state") == "open" and a.get("severity") != "info"]
    if alerts:
        parts.append(f"{len(alerts)} open warnings, e.g. {alerts[0]['message']}")
    return " ".join(parts)


explainer = Explainer()
