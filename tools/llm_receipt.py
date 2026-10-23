"""Per-response, allowlisted provider facts; never inferred from generated JSON."""
from dataclasses import dataclass
import hashlib
from typing import Any


@dataclass(frozen=True)
class JSONCompletion:
    data: Any
    provider: dict


def completion_metadata(response):
    choices = getattr(response, 'choices', None)
    choices = choices if isinstance(choices, list) else []
    choice = choices[0] if len(choices) == 1 else None
    message = getattr(choice, 'message', None)
    reason = getattr(choice, 'finish_reason', None)
    reason = reason if reason in ('stop', 'length', 'content_filter', 'tool_calls', 'function_call') else 'unknown'
    refusal = bool(getattr(message, 'refusal', None))
    tools = bool(getattr(message, 'tool_calls', None) or getattr(message, 'function_call', None))
    content_valid = isinstance(getattr(message, 'content', None), str)
    status = ('invalid_choices' if len(choices) != 1 else 'refused' if refusal else
              'tool_call' if tools else 'complete' if reason == 'stop' and content_valid else
              'truncated' if reason == 'length' else 'filtered' if reason == 'content_filter' else 'incomplete')
    usage = getattr(response, 'usage', None)
    counts = {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        value = getattr(usage, key, None)
        counts[key] = value if type(value) is int and 0 <= value <= 2**53-1 else None
    if all(value is not None for value in counts.values()) and (
            counts['prompt_tokens'] + counts['completion_tokens'] != counts['total_tokens']):
        counts = dict.fromkeys(counts)
    out = dict(schema_version=1, source='provider_response', completion_status=status,
               finish_reason=reason, refusal_present=refusal, tool_call_present=tools, usage=counts)
    for key in ('model', 'system_fingerprint'):
        value = getattr(response, key, None)
        out[key+'_sha256'] = hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) and value else None
    return out
