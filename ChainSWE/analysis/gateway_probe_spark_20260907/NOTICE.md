# Classification notice

This directory was produced by the first version of the probe. Its raw HTTP
responses are valid and contain native `tool_calls` for both `auto` and
`forced`, but the first-version classifier incorrectly reported those cases
when the local OpenAI SDK was unavailable.

Use the raw response bodies as evidence, or use the corrected results in:

`../gateway_probe_spark_sweedit_tools_20260907/`
