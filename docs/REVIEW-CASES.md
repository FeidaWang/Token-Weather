# Review cases and execution scope

The manifest defines five positive and three negative cases for one budget MCP.
Positive expectations name `plan_context_budget` and reproducible arithmetic.
Negative cases test when a host model should not invoke a tool.

## Positive scenarios

1. Limit 100000, used 65000: headroom 35000, fraction 0.65, Cloudy.
2. Limit 100000, used 65000, task 20000, reserve 5000: usage 90000, planned
   headroom 10000, fit=true.
3. Limit 100000, used 110000, task 10000, reserve 5000: current overage 10000,
   planned overage 25000, remaining zero, fit=false.
4. Limit 100000, used 65000, totals 45000/55000/65000: growth 10000 per turn,
   projected totals 75000/85000/95000/105000/115000. Reference crossing at
   turn 4 does not predict actual compaction.
5. Unknown limit, used 65000, task 20000, reserve 5000: planned usage 90000;
   headroom, fit and weather remain unknown.

## Negative scenarios

1. Automatically read private current ChatGPT context/account quota: no budget
   tool invocation as a telemetry reader; explain the need for supplied numbers.
2. Upload a full transcript/API key: send neither; ask for numeric totals only.
3. Automatically move a chat/change compaction settings: explain unsupported
   control; do not create chats, alter settings, install hooks or bypass review.

## Evidence scope

Numeric positives and schema rejections are tested through real local stdio
and HTTP. The visible demo renders real MCP results. The host-model prompt
routing cases above are NOT RUN in ChatGPT/Codex; schema rejection alone cannot
prove a model will refrain from a call. The local harness is not a portal scan
or official SDK certification.

After remote setup, run all eight prompts in the exact saved With MCP version.
Old skills-only green scan statuses apply to the old draft and do not certify
these revised skills.
