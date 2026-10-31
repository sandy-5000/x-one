---
title: "Prompt engineering"
order: 2
---
# Prompt Engineering

Prompt engineering is writing inputs that reliably get the output you want. Treat a prompt like a **specification for a smart new colleague** who has no context about your project.

## Principles

1. **Be explicit.** State the task, the audience, the format, and the constraints. "Summarise" is vague; "Summarise in 3 bullet points for a non-technical manager, under 60 words" is not.
2. **Give context and the reason.** "Keep it short because it's shown in a mobile notification" works better than "keep it short".
3. **Show examples.** One to five input → output pairs (few-shot) beat paragraphs of description.
4. **Separate instructions from data.** Wrap documents in clear delimiters such as XML tags.
5. **Say what to do, not only what not to do.** "Write in plain prose paragraphs" beats "don't use markdown".
6. **Ask for structure** you can parse — JSON with a schema, or tagged sections.
7. **Iterate against test cases**, not single examples.

## Anatomy of a good prompt

```text
<role>
You are a senior support engineer for a payments API.
</role>

<task>
Classify the customer ticket below and draft a first reply.
</task>

<rules>
- Category must be one of: billing, integration, outage, account, other.
- Reply in under 120 words, friendly and specific.
- If the ticket mentions a charge failure, ask for the payment ID.
</rules>

<ticket>
{{ticket_text}}
</ticket>

<output_format>
Return JSON: {"category": "...", "urgency": 1-5, "reply": "..."}
</output_format>
```

## Techniques

### Few-shot examples

```text
Convert each product review into a sentiment label.

Review: "Arrived broken, refund took 3 weeks."   → negative
Review: "Does the job, nothing special."           → neutral
Review: "Best purchase this year!"                 → positive

Review: "Battery died after two days."             →
```

Make examples diverse and representative; the model copies their length, tone, and quirks.

### Chain of thought

For multi-step problems, let the model reason before answering:

```text
Solve the problem. First think through it step by step inside <thinking> tags,
then give only the final number inside <answer> tags.
```

Modern reasoning models already think internally; for them, describe the goal and constraints clearly rather than scripting every step.

### Prompt chaining

Split a big task into a pipeline of smaller prompts, passing outputs forward:

```text
extract facts ──► check facts against source ──► write summary ──► format as email
```

Each step is easier to test and debug than one giant prompt.

### Structured output

Most APIs can force output to match a JSON Schema ("structured outputs" / "JSON mode"). Prefer that over asking nicely and parsing with regex.

```python
from pydantic import BaseModel

class Ticket(BaseModel):
    category: str
    urgency: int
    reply: str

# Many SDKs accept the schema directly and return a validated object.
result = client.responses.parse(model=MODEL, input=prompt, text_format=Ticket)
ticket: Ticket = result.output_parsed
```

### Prefilling and stop sequences

Starting the assistant's reply for it (for example with `{`) or setting a stop string keeps the output on format.

## Common mistakes

- Contradictory instructions buried in a long prompt.
- Examples that all look alike, so the model overfits to them.
- Asking for "no hallucinations" instead of supplying sources and allowing "I don't know".
- Putting critical rules only at the very top of a huge context — repeat key constraints near the end.
- Never testing: changing the prompt and eyeballing one output.

## Testing prompts

Build a small **eval set** — 20 to 200 realistic inputs with expected outputs or grading criteria — and run every prompt change against it. See the Evals and guardrails page.
