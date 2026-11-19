"""Distinguish variable log fields from literal evidence when grouping alerts."""
import re

from agents.alert_numeric import FIELD_END, FIELD_START, MS_FIELD, MS_WAIT, milliseconds


ID_FIELDS = ("request_id", "trace_id", "span_id", "correlation_id")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
_MISSING_IDS = {"none", "null", "unknown", "missing", "undefined", "unset"}
_UUID_OR_HASH = r"(?:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|[0-9a-f]{24,64})"
_VARIABLE = re.compile("|".join((
    FIELD_START + r"(?P<id_field>" + "|".join(ID_FIELDS) + r")=(?P<id_value>[^\s,;&]+)" + FIELD_END,
    r"\brequest\s+(?P<request>" + _UUID_OR_HASH + r")\b",
    MS_FIELD.pattern,
    MS_WAIT.pattern,
)), re.I)


def variable_id(value):
    if type(value) is int:
        return value >= 0
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and value.lower() not in _MISSING_IDS


def log_template(message):
    """Alternating literals and slots keep a printed <VAR> distinct from a mask."""
    parts = []
    end = 0
    for match in _VARIABLE.finditer(message):
        if match["id_field"]:
            value = match["id_value"]
            quote = value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else ""
            if not variable_id(value[1:-1] if quote else value):
                continue
            slot = match["id_field"].lower() + "=" + quote + "<VAR>" + quote
        elif match["request"]:
            slot = "request <VAR>"
        elif match["ms_field"]:
            if milliseconds(match["ms_value"]) is None:
                continue
            slot = match["ms_field"].lower() + "=<VAR>"
        else:
            if milliseconds(match["elapsed"]) is None:
                continue
            slot = match["wait"].lower() + " <VAR> " + match["unit"].lower()
        parts.extend((message[end:match.start()], slot))
        end = match.end()
    if not parts:
        return message, message
    parts.append(message[end:])
    return ("masked_log", tuple(parts)), "".join(parts)
