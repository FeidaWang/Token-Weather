"""Pure token-budget arithmetic; no filesystem, network, account or model access."""
from fractions import Fraction

MAX_TOKENS = 10**12
COUNTS = (
    "reference_limit_tokens", "used_tokens", "planned_increase_tokens",
    "reserved_output_tokens",
)
FIELDS = COUNTS + ("recent_turn_tokens",)
BASES = ("user_provided", "assumed", "supplied_measurement")
WEATHER = ("Clear", "Fair", "Cloudy", "Showers", "Storm")


def count_schema(minimum=0):
    return {"type": ["integer", "null"], "minimum": minimum, "maximum": MAX_TOKENS}


INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "reference_limit_tokens": dict(count_schema(1), description="Explicit reference budget; never inferred from a platform or model name."),
        "used_tokens": dict(count_schema(), description="Explicit current token estimate. Unknown may be omitted or null."),
        "planned_increase_tokens": dict(count_schema(), description="Estimated extra context for the next task, excluding the separate output reserve."),
        "reserved_output_tokens": dict(count_schema(), description="Additional output reserve. Use explicit zero only when intended."),
        "recent_turn_tokens": {
            "type": ["array", "null"], "maxItems": 6,
            "items": {"type": "integer", "minimum": 0, "maximum": MAX_TOKENS},
            "description": "Chronological turn-end totals, at most six. Last must equal used_tokens when both are provided. Decreases reset the growth sample.",
        },
        "sources": {
            "type": "object", "additionalProperties": False,
            "properties": {field: {"type": "string", "enum": list(BASES)} for field in FIELDS},
            "description": "Optional provenance per supplied field. Default user_provided does not mean verified telemetry.",
        },
    },
}


def object_schema(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


NULL_NUMBER = {"type": ["number", "null"]}
NULL_COUNT = {"type": ["integer", "null"], "minimum": 0}
METRICS_SCHEMA = object_schema({
    "used_tokens": NULL_NUMBER,
    "fraction": NULL_NUMBER,
    "remaining_tokens": NULL_NUMBER,
    "over_budget_tokens": NULL_NUMBER,
    "weather": {"type": "string", "enum": ["Unknown", *WEATHER]},
})
OUTPUT_SCHEMA = object_schema({
    "schema_version": {"const": 1},
    "basis": {"const": "explicit_numeric_inputs"},
    "reference_limit_tokens": NULL_COUNT,
    "inputs": object_schema({field: INPUT_SCHEMA["properties"][field] for field in FIELDS}),
    "sources": object_schema({field: {"type": ["string", "null"], "enum": [None, *BASES]} for field in FIELDS}),
    "current": METRICS_SCHEMA,
    "plan": object_schema({
        "added_tokens": NULL_COUNT,
        "after": METRICS_SCHEMA,
        "fits_reference": {"type": ["boolean", "null"]},
    }),
    "growth": object_schema({
        "tokens_per_turn": NULL_NUMBER,
        "intervals": {"type": "integer", "minimum": 0, "maximum": 5},
        "turns_until_reference": NULL_COUNT,
        "forecast": {"type": "array", "maxItems": 5, "items": object_schema({
            "step": {"type": "integer", "minimum": 1, "maximum": 5},
            "budget": METRICS_SCHEMA,
        })},
    }),
    "unknowns": {"type": "array", "items": {"type": "string", "enum": [
        "reference_limit_tokens", "used_tokens", "planned_increase_tokens", "reserved_output_tokens",
        "growth_insufficient_turns", "actual_platform_context", "actual_compaction_policy", "account_quota", "monetary_cost",
    ]}},
    "notices": {"type": "array", "items": {"type": "string", "enum": ["recent_decrease_resets_growth"]}},
    "limitations": {"type": "array", "items": {"type": "string"}},
})


class BudgetInputError(ValueError):
    pass


def token_integer(value, minimum=0):
    # JSON Schema integer also accepts integral JSON numbers such as 65000.0.
    if type(value) not in (int, float) or not minimum <= value <= MAX_TOKENS or (type(value) is float and not value.is_integer()):
        raise BudgetInputError("Token counts must be finite integers within the documented bounds.")
    return int(value)


def validate(arguments):
    # Error text never echoes unknown field names or user-supplied values.
    if type(arguments) is not dict or set(arguments) - set(FIELDS) - {"sources"}:
        raise BudgetInputError("Only documented numeric fields and source enums are accepted.")
    data = {field: arguments.get(field) for field in FIELDS}
    for field in COUNTS:
        value = data[field]
        minimum = 1 if field == "reference_limit_tokens" else 0
        if value is not None:
            try:
                data[field] = token_integer(value, minimum)
            except BudgetInputError:
                raise BudgetInputError(field + " must be an integer within its documented bounds, or null.") from None
    recent = data["recent_turn_tokens"]
    if recent is not None:
        if type(recent) is not list or len(recent) > 6:
            raise BudgetInputError("recent_turn_tokens must contain at most six nonnegative integer totals.")
        recent = [token_integer(n) for n in recent]
        data["recent_turn_tokens"] = recent
        if recent and data["used_tokens"] is not None and recent[-1] != data["used_tokens"]:
            raise BudgetInputError("The last recent turn total must equal used_tokens; use compatible observations.")
    sources = arguments.get("sources", {})
    if type(sources) is not dict or set(sources) - set(FIELDS):
        raise BudgetInputError("Sources may describe only documented input fields.")
    if any(type(value) is not str or value not in BASES or data[field] is None for field, value in sources.items()):
        raise BudgetInputError("Use a documented source enum only for a supplied field.")
    return data, {field: sources.get(field, "user_provided") if data[field] is not None else None for field in FIELDS}


def number(value):
    return int(value) if value.denominator == 1 else float(value)


def metrics(used, limit):
    result = {"used_tokens": number(Fraction(used)) if used is not None else None,
              "fraction": None, "remaining_tokens": None, "over_budget_tokens": None, "weather": "Unknown"}
    if used is not None and limit is not None:
        ratio = Fraction(used, limit)
        result.update(fraction=float(ratio), remaining_tokens=number(max(Fraction(0), limit - used)),
                      over_budget_tokens=number(max(Fraction(0), used - limit)),
                      weather=WEATHER[next((i for i, cut in enumerate((35, 55, 70, 85)) if ratio * 100 < cut), 4)])
    return result


def plan_context_budget(arguments):
    data, sources = validate(arguments)
    limit, used = data["reference_limit_tokens"], data["used_tokens"]
    increase, reserve = data["planned_increase_tokens"], data["reserved_output_tokens"]
    added = increase + reserve if increase is not None and reserve is not None else None
    after = used + added if used is not None and added is not None else None
    recent = list(data["recent_turn_tokens"] or [])
    notices = []
    decreases = [i for i in range(1, len(recent)) if recent[i] < recent[i - 1]]
    if decreases:
        recent = recent[decreases[-1]:]
        notices.append("recent_decrease_resets_growth")
    intervals = max(0, len(recent) - 1)
    rate = Fraction(recent[-1] - recent[0], intervals) if intervals >= 2 else None
    until = None
    forecast = []
    if rate is not None and used is not None and limit is not None:
        if used >= limit:
            until = 0
        elif rate > 0:
            distance = Fraction(limit - used, 1) / rate
            until = -(-distance.numerator // distance.denominator)
        forecast = [{"step": step, "budget": metrics(Fraction(used) + rate * step, limit)} for step in range(1, 6)]
    unknowns = [field for field in COUNTS if data[field] is None]
    if rate is None:
        unknowns.append("growth_insufficient_turns")
    unknowns.extend(("actual_platform_context", "actual_compaction_policy", "account_quota", "monetary_cost"))
    return {
        "schema_version": 1, "basis": "explicit_numeric_inputs", "reference_limit_tokens": limit,
        "inputs": data, "sources": sources, "current": metrics(used, limit),
        "plan": {"added_tokens": added, "after": metrics(after, limit),
                 "fits_reference": after <= limit if after is not None and limit is not None else None},
        "growth": {"tokens_per_turn": number(rate) if rate is not None else None,
                   "intervals": intervals, "turns_until_reference": until, "forecast": forecast},
        "unknowns": unknowns, "notices": notices,
        "limitations": [
            "Arithmetic from supplied numbers; no access to current ChatGPT or Codex telemetry.",
            "Weather uses reference-budget cuts at 35%, 55%, 70% and 85%; it is not a platform policy.",
            "Forecast extends the mean change across at least two consecutive intervals after the last decrease; includes zero-growth intervals.",
            "Forecast starts from current usage and excludes the one-off next-task plan. No guaranteed compaction timing.",
            "Missing estimates stay unknown. No account quota or currency pricing is inferred.",
        ],
    }
