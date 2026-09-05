"""Gemini provider adapter used by the agent pipelines.

Provider-specific SDK calls and response shaping live here so the proposer and
strategist only depend on ``complete(prompt, payload)``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOTENV_PATH = ROOT / ".env"
DEFAULT_RESPONSE_LOG = ROOT / "logs" / "gemini_responses.jsonl"
MAX_PARSE_ATTEMPTS = 3


class ModelResponseError(RuntimeError):
    """The provider did not produce a response matching the requested JSON shape."""


@dataclass(frozen=True)
class ModelClientConfig:
    """Provider configuration; the model slug is deliberately not hard-coded."""

    provider: str = "gemini"
    model_slug: str | None = None
    api_key_env_var: str = "GEMINI_API_KEY"
    model_env_var: str = "GEMINI_MODEL"
    dotenv_path: Path = DEFAULT_DOTENV_PATH
    response_log_path: Path = DEFAULT_RESPONSE_LOG
    max_parse_attempts: int = MAX_PARSE_ATTEMPTS
    max_rate_limit_retries: int = 5
    rate_limit_backoff_seconds: float = 1.0


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without overwriting the process environment."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


def _raw_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(response, Mapping):
        candidate_text = response.get("text")
        if isinstance(candidate_text, str):
            return candidate_text
    raise ModelResponseError("Gemini response did not contain text")


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.IGNORECASE)
    return match.group(1).strip() if match else stripped


def _response_schema(kind: str) -> dict[str, Any]:
    if kind == "proposal":
        properties = {
            "customer_id": {"type": "string"},
            "option_id": {"type": "string"},
            "offer_type": {"type": "string"},
            "target_tier": {"type": "string"},
            "target_addon": {"type": "string"},
            "proposed_discount": {"type": "string"},
            "proposed_seats": {"type": "integer"},
            "campaign_id": {"type": "string"},
            "reason": {"type": "string"},
        }
        required = ["proposed_discount", "reason"]
    elif kind == "strategy":
        properties = {
            "approach": {"type": "string"},
            "option_id": {"type": "string"},
            "offer_type": {"type": "string"},
            "target_tier": {"type": "string"},
            "target_addon": {"type": "string"},
            "proposed_seats": {"type": "integer"},
            "discount_min": {"type": "string"},
            "discount_max": {"type": "string"},
            "proposed_discount": {"type": "string"},
            "campaign_id": {"type": "string"},
        }
        required = ["approach"]
    else:
        raise ModelResponseError(f"unknown response kind: {kind!r}")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _validate_json_object(text: str, kind: str) -> dict[str, Any]:
    cleaned = _strip_markdown_fence(text)
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ModelResponseError(
            f"response was not one JSON object: {exc.msg} at character {exc.pos}"
        ) from exc
    if not isinstance(decoded, dict):
        raise ModelResponseError("response JSON must be an object")

    schema = _response_schema(kind)
    unknown = sorted(set(decoded) - set(schema["properties"]))
    if unknown:
        raise ModelResponseError(f"response contains extra field(s): {', '.join(unknown)}")
    for field in schema["required"]:
        if field not in decoded or decoded[field] is None:
            raise ModelResponseError(f"response is missing required field: {field}")

    reason = decoded.get("reason")
    if kind == "proposal" and (not isinstance(reason, str) or not reason.strip()):
        raise ModelResponseError("proposal reason must be a non-empty string")
    if kind == "strategy" and (not isinstance(decoded["approach"], str) or not decoded["approach"].strip()):
        raise ModelResponseError("strategy approach must be a non-empty string")

    if "proposed_discount" in decoded:
        _validate_discount(decoded["proposed_discount"], "proposed_discount")
    for field in ("discount_min", "discount_max"):
        if field in decoded:
            _validate_discount(decoded[field], field)

    if kind == "proposal" and "option_id" not in decoded and "offer_type" not in decoded:
        raise ModelResponseError("proposal requires option_id or offer_type")
    if kind == "strategy" and "option_id" not in decoded and "offer_type" not in decoded:
        raise ModelResponseError("strategy requires option_id or offer_type")
    return decoded


def _validate_discount(value: Any, field: str) -> None:
    if isinstance(value, bool):
        raise ModelResponseError(f"{field} must be a numeric discount")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ModelResponseError(f"{field} must be a numeric discount") from exc
    if not parsed.is_finite():
        raise ModelResponseError(f"{field} must be finite")


class GeminiModelClient:
    """Gemini implementation of the injected model-client interface."""

    def __init__(
        self,
        config: ModelClientConfig | None = None,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or ModelClientConfig()
        if self.config.provider != "gemini":
            raise ValueError(f"unsupported model provider: {self.config.provider!r}")
        if self.config.max_parse_attempts < 1:
            raise ValueError("max_parse_attempts must be positive")
        _load_dotenv(Path(self.config.dotenv_path))
        api_key = os.getenv(self.config.api_key_env_var, "").strip()
        if not api_key:
            raise RuntimeError(
                f"{self.config.api_key_env_var} is missing or empty; add it to {self.config.dotenv_path}"
            )
        model_slug = self.config.model_slug or os.getenv(self.config.model_env_var, "").strip()
        if not model_slug:
            raise RuntimeError(
                f"{self.config.model_env_var} is missing or empty; configure the Gemini model slug"
            )
        self.model_slug = model_slug
        self._sleep = sleep
        self.failures: list[str] = []
        if client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError(
                    "Gemini SDK is unavailable; install the google-genai package"
                ) from exc
            client = genai.Client(api_key=api_key)
        self.client = client

    def complete(self, prompt: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Call Gemini, retry malformed JSON, and return a validated object."""

        kind = str(payload.get("_response_kind", "proposal"))
        request_prompt = self._prompt_with_payload(prompt, payload)
        last_error: str | None = None
        for attempt in range(1, self.config.max_parse_attempts + 1):
            response = self._generate(request_prompt, kind)
            raw = _raw_text(response)
            self._log_raw_response(raw, kind, attempt)
            try:
                return _validate_json_object(raw, kind)
            except ModelResponseError as exc:
                last_error = str(exc)
                if attempt < self.config.max_parse_attempts:
                    request_prompt = (
                        prompt
                        + "\n"
                        + self._prompt_with_payload("", payload)
                        + "\nYour previous response was malformed: "
                        + last_error
                        + ". Return one corrected JSON object only."
                    )
        message = f"Gemini response failed validation after {self.config.max_parse_attempts} attempts: {last_error}"
        self.failures.append(message)
        raise ModelResponseError(message)

    def _prompt_with_payload(self, prompt: str, payload: Mapping[str, Any]) -> str:
        visible_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        return prompt + "\nAccount or cohort data:\n" + json.dumps(
            visible_payload, default=str, sort_keys=True
        )

    def _generate(self, prompt: str, kind: str) -> Any:
        config: Any = None
        try:
            from google.genai import types

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=_response_schema(kind),
            )
        except (ImportError, AttributeError, TypeError):
            # Older SDKs may not expose structured response configuration.
            config = None

        rate_attempt = 0
        while True:
            try:
                kwargs = {"model": self.model_slug, "contents": prompt}
                if config is not None:
                    kwargs["config"] = config
                return self.client.models.generate_content(**kwargs)
            except Exception as exc:
                if not self._is_rate_limit(exc) or rate_attempt >= self.config.max_rate_limit_retries:
                    raise
                self._sleep(self.config.rate_limit_backoff_seconds * (2**rate_attempt))
                rate_attempt += 1

    def _log_raw_response(self, raw: str, kind: str, attempt: int) -> None:
        path = Path(self.config.response_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": self.config.provider,
            "model": self.model_slug,
            "kind": kind,
            "attempt": attempt,
            "raw_response": raw,
        }
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        status = getattr(exc, "status_code", getattr(exc, "code", None))
        text = str(exc).lower()
        return status == 429 or "rate limit" in text or "resource exhausted" in text or "too many requests" in text


__all__ = [
    "DEFAULT_DOTENV_PATH",
    "DEFAULT_RESPONSE_LOG",
    "GeminiModelClient",
    "MAX_PARSE_ATTEMPTS",
    "ModelClientConfig",
    "ModelResponseError",
]
