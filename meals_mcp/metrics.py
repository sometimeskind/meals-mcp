"""Prometheus metrics, served on their own port so /metrics never sits behind the edge."""

from prometheus_client import Counter

TOOL_CALLS = Counter("meals_tool_calls_total", "MCP tool calls", ["tool", "outcome"])
GOOGLE_AUTH_FAILURES = Counter(
    "meals_google_auth_failures_total", "Google token refreshes or calls rejected as unauthorised"
)
RATE_LIMITED = Counter(
    "meals_rate_limited_total", "Writes refused by a per-user hourly limit", ["kind"]
)
