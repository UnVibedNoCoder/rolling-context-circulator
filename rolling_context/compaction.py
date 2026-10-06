"""Bounded compactor failures and final-content checks; never retain model reasoning."""
import json
import math
import re
import time
import urllib.error

_MESSAGES = {
    "evidence_overstated": "Compactor success claim exceeds cited visible evidence; RAW retained",
    "incomplete_output": "Compactor did not finish the final structured output; RAW retained",
    "visible_reasoning": "Compactor leaked reasoning into visible content; RAW retained",
    "unexpected_reasoning": "Baseline compactor returned reasoning despite thinking being disabled",
    "json_invalid": "Compactor final content is not valid JSON",
    "schema_invalid": "Compactor final content failed strict schema validation",
    "provenance_invalid": "Compactor referenced an unknown or invalid source ID",
    "memory_budget": "Compactor summary exceeds the stored memory budget",
    "timeout": "Compactor request exceeded its allotted time",
    "response_invalid": "Compactor returned an invalid response envelope",
    "configuration_invalid": "Compactor generation or deadline allowance is invalid",
    "backend_error": "Compactor backend request failed",
    "internal_error": "Compactor failed; payload and exception detail withheld",
}
_THINK = re.compile(r"<\s*/?\s*think\b[^>]*>", re.IGNORECASE)


class CompactionFailure(ValueError):
    def __init__(self, code, mode, *, finish_reason=None, fallback_used=False,
                 librarian_error_code=None):
        self.code = code if code in _MESSAGES else "internal_error"
        self.mode = mode if mode in {"librarian", "baseline", "preflight"} else "preflight"
        self.finish_reason = finish_reason if finish_reason in {"stop", "length", "error", "tool_calls"} else None
        self.fallback_used = bool(fallback_used)
        self.librarian_error_code = librarian_error_code if librarian_error_code in _MESSAGES else None
        super().__init__(_MESSAGES[self.code])


def failure(exc, mode, **kwargs):
    if isinstance(exc, CompactionFailure):
        return CompactionFailure(exc.code, exc.mode, finish_reason=exc.finish_reason, **kwargs)
    if isinstance(exc, TimeoutError):
        code = "timeout"
    elif isinstance(exc, urllib.error.URLError):
        code = "timeout" if isinstance(exc.reason, TimeoutError) else "backend_error"
    elif isinstance(exc, OSError):
        code = "backend_error"
    elif isinstance(exc, json.JSONDecodeError):
        code = "json_invalid"
    else:
        code = "internal_error"
    return CompactionFailure(code, mode, **kwargs)


def failure_fields(exc, mode="preflight"):
    safe = exc if isinstance(exc, CompactionFailure) else failure(exc, mode)
    fields = {"error_type": type(exc).__name__[:80], "error_code": safe.code,
              "error_message": str(safe), "failure_stage": safe.mode,
              "fallback_used": safe.fallback_used}
    if safe.finish_reason:
        fields["finish_reason"] = safe.finish_reason
    if safe.librarian_error_code:
        fields["librarian_error_code"] = safe.librarian_error_code
    return fields


def request_timeout(settings, started, mode):
    total = float(settings["job_timeout"])
    reserve = min(float(settings.get("librarian_fallback_reserve_seconds", 180)), total / 3)
    if not math.isfinite(total) or total <= 0 or not math.isfinite(reserve) or reserve <= 0:
        raise CompactionFailure("configuration_invalid", mode)
    remaining = total - (time.monotonic() - started)
    if mode == "librarian":
        remaining -= reserve
    if remaining <= 0:
        raise CompactionFailure("timeout", mode)
    return remaining


def reject_visible_reasoning(text, mode):
    if isinstance(text, str) and _THINK.search(text):
        raise CompactionFailure("visible_reasoning", mode)


def final_content(result, mode):
    try:
        choice = result["choices"][0]
        message = choice["message"]
        # Discard even if validation subsequently fails. Production SSE never
        # collects Librarian reasoning; this also protects non-stream adapters.
        channels = ("reasoning_content", "reasoning", "reasoning_details", "codex_reasoning_items",
                    "_thinking_prefill", "thinking", "thinking_content", "reasoning_present")
        reasoning = any([bool(message.pop(key, None)) for key in channels])
        text = message.get("content")
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise CompactionFailure("response_invalid", mode) from None
    if not isinstance(text, str):
        raise CompactionFailure("response_invalid", mode)
    reject_visible_reasoning(text, mode)
    if mode == "baseline" and reasoning:
        raise CompactionFailure("unexpected_reasoning", mode)
    if finish != "stop":
        raise CompactionFailure("incomplete_output", mode, finish_reason=finish)
    return text


def validated_content(text, source_ids, validator, mode):
    try:
        return validator(text, source_ids)
    except CompactionFailure:
        raise
    except json.JSONDecodeError:
        raise CompactionFailure("json_invalid", mode) from None
    except (ValueError, TypeError, KeyError) as exc:
        # Validator strings are our own fixed messages, never model text.
        code = "provenance_invalid" if str(exc) in {
            "Unknown librarian provenance", "Unknown summary provenance"} else "schema_invalid"
        raise CompactionFailure(code, mode) from None


def safe_timings(timings):
    # Accept known numeric server metrics only, never arbitrary response fields.
    keys = {"prompt_n", "prompt_ms", "prompt_per_token_ms", "prompt_per_second",
            "predicted_n", "predicted_ms", "predicted_per_token_ms", "predicted_per_second",
            "cache_n", "draft_n", "draft_n_accepted"}
    if not isinstance(timings, dict):
        return None
    return {k: v for k, v in timings.items() if k in keys and type(v) in (int, float)
            and math.isfinite(v)}


def safe_usage(usage):
    if not isinstance(usage, dict):
        return {}
    return {k: v for k, v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and type(v) is int and v >= 0}
