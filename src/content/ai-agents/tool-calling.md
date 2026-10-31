---
title: "Tool calling"
order: 4
---
# Tool Calling (Function Calling)

Tool calling lets a model **ask your code to run a function** — search the web, query a database, send an email — and then use the result. The model never executes anything itself; it emits a structured request, and your application decides whether and how to run it.

## The round trip

```text
1. app ──► model:  user message + list of tool definitions
2. model ──► app:  "call get_weather with {city: 'Pune'}"
3. app:            runs get_weather("Pune") → {"temp_c": 31, "sky": "clear"}
4. app ──► model:  the tool result
5. model ──► app:  "It's 31 °C and clear in Pune right now."
```

## Defining a tool

A tool is a name, a description, and a JSON Schema for its arguments.

```json
{
  "name": "get_weather",
  "description": "Get the current weather for a city. Use when the user asks about weather or temperature.",
  "input_schema": {
    "type": "object",
    "properties": {
      "city": { "type": "string", "description": "City name, e.g. 'Pune'" },
      "unit": { "type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius" }
    },
    "required": ["city"]
  }
}
```

The **description is the documentation the model reads** to decide when and how to use the tool. Write it like you would for a new teammate: what it does, when to use it, what it returns, and edge cases.

## The tool loop in Python

```python
messages = [{"role": "user", "content": "Is it warm in Pune today?"}]

while True:
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason != "tool_use":
        break                                   # final answer

    results = []
    for block in response.content:
        if block.type == "tool_use":
            output = run_tool(block.name, block.input)       # your code
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(output),
            })
    messages.append({"role": "user", "content": results})

print(response.content[0].text)
```

Every provider has the same shape with different field names. This loop — **call model, run requested tools, feed results back, repeat** — is the core of every agent.

## Designing good tools

- **Few, well-scoped tools** beat many overlapping ones. Ten similar tools confuse the model.
- **Clear names:** `search_orders`, not `query2`.
- **Strict schemas:** enums, required fields, formats. Validate inputs anyway.
- **Return useful, compact results.** Don't dump a 5 MB JSON blob; return the fields that matter, with pagination.
- **Actionable errors:** `"No order with id 123. Order ids look like 'ORD-xxxxx'."` lets the model fix its call; a stack trace doesn't.
- **Idempotent where possible**, so retries are safe.
- **Parallel calls:** models can request several independent tools at once; run them concurrently.

## Safety

- Treat tool arguments as **untrusted input** — they can be steered by prompt injection in documents the model read.
- Require **human confirmation** for destructive or costly actions (delete, pay, send email to customers).
- Scope credentials to the minimum: a read-only DB user for a reporting tool.
- Log every call with its arguments and result for auditing.

## From tools to MCP

Hard-coding tools into each app doesn't scale: every app re-implements "GitHub tools", "Postgres tools", and so on. The **Model Context Protocol** standardises how tools (and other context) are exposed, so one server works with any compatible client. See the MCP page.
