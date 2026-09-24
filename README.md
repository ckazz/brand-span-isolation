# What does a session-scoped judge receive for each turn?

This repo answers that question by measurement rather than by explanation. It sends a three-turn
conversation to a Galileo deployment over OpenTelemetry, reads back what the deployment stored as
each turn's input and output, and prints the reduction from the full OTLP payload down to the text a
session-scoped judge is given.

It assumes the structure of a multi-turn chatbot: one session per conversation, linked with
`gen_ai.conversation.id`, one trace per turn, an agent parent span per turn with LLM and tool spans
nested under it, and ingestion straight over OTLP with no Galileo SDK in the application.

## Why the shape of the export matters

OpenTelemetry has no trace object. A deployment receives spans, so a trace's `input` and `output` are
derived from them at ingestion. The turn's root span is used when it carries message content. When it
carries none, the first nested span with content is used instead, and in an agent turn that is often
the LLM step that decided to call a tool rather than the step that produced the customer-visible
reply.

That matters because the derived values are stored as the turn's input and output. A metric reading
only per-turn input and output reads whatever was stored, so if the wrong text was stored at
ingestion, no metric configuration can recover the real reply afterwards. This is the one thing worth
verifying before a session-level judge is put in front of a brand or tone question.

## Requirements

Python 3.9 or newer. Standard library only, so there is nothing to install and no Galileo SDK
version to pin.

Live mode additionally needs a deployment URL and an API key. Copy `.env.sample` to `.env`, fill it
in, then:

```
set -a; source .env; set +a
```

## Running it

```
python3 turn_io_synthesis.py                          # inspect, no network, no credentials
python3 turn_io_synthesis.py --mode live              # post, read back, check
python3 turn_io_synthesis.py --mode live --only s1    # one scenario
python3 turn_io_synthesis.py --mode live --brief      # skip the full payload dumps
```

Inspect mode describes what would be sent and makes no claim about the result. Live mode is where
the behaviour is actually tested. It writes to a fresh log stream per scenario per run, named after
the log stream in your environment plus the scenario and a run tag, so repeated runs sit beside each
other instead of on top of each other. Every scenario prints a check grid, and the process exits
non-zero if any check fails.

Live mode creates the project and log streams on first ingest if they do not exist. Point it at a
throwaway log stream: the conversations are synthetic.

## The three scenarios

All three are the same three-turn conversation, the same four spans per turn, and the same
`gen_ai.conversation.id`. The only difference is where the turn's message attributes sit.

| id | what the turn's root span carries | what it is for |
|----|-----------------------------------|----------------|
| s1 | `gen_ai.input.messages` and `gen_ai.output.messages` | the shape currently reported as being sent |
| s2 | no message attributes; only the nested LLM and tool spans have content | the counterfactual, and the regression to watch if instrumentation changes |
| s3 | indexed `gen_ai.prompt.N` and `gen_ai.completion.N` | an alternative convention some instrumentation emits |

Every turn carries a tool span with call arguments and a result wrapped in a deliberately bulky
compliance and audit block, because that is the noise a session-level judge should never be paying
for.

The conversation text uses obvious sentinels (`USER-T1`, `REPLY-T1`, and an intermediate tool-calling
step) so a wrong answer is visible on sight rather than needing a careful read.

## What a run prints

1. What is sent. The complete OTLP export request per turn: every span, every attribute, the tool
   call arguments, the tool result and the audit block.
2. What was stored as the turn. The persisted `input` and `output` read back through the API.
3. What a judge receives. With a session-scoped metric set to read only trace input and output, an
   ordered per-turn list of input and output, and nothing else.

## Results from a verification run

Observed on 2026-09-17 against a deployment on the 1.11x line.

| id | sent | stored as turn text | judge input | turn output |
|----|------|---------------------|-------------|-------------|
| s1 | 11,971 chars | 238 | 379 | the final assistant reply |
| s2 | 10,905 chars | 257 | 398 | the intermediate tool-calling step, not the reply |
| s3 | 11,920 chars | 238 | 379 | the final assistant reply |

Judge input is roughly 3 percent of what was sent in all three cases. The tool call arguments and
the audit block are absent from it in all three cases: the setting removes span detail regardless of
which scenario ran.

Two details worth noting from s2. First, the turn's input was still correct and only the output was
wrong, so a wrong result here does not announce itself by looking empty or broken. Second, the
promoted step's own text names the tool it was about to call, so a little tool detail reaches the
judge through the turn output itself rather than through span detail.

## Scope

No judge runs here and no LLM is called. The question this answers is what a judge would be given,
which is answered without scoring anything.

Reading only per-turn input and output is a metric input setting. It is set through the API today
rather than in the console, so a metric configured this way is created and updated over the API. The
call sequence is in the next section.

The scenarios exercise ingestion and read-back only. Nothing here changes or measures scoring
behaviour, cost or latency.

## Creating the metric over the API

The console has no control for the per-turn input setting, so a metric that reads only each turn's
input and output is created and maintained over the API. The sequence below was run against a
deployment on the 1.11x line on 2026-09-23, and the behaviour described is what it returned.

**1. Create the metric.** `POST /scorers`

```json
{
  "name": "brand-tone-and-language-quality",
  "description": "Is the assistant on brand for the whole conversation?",
  "scorer_type": "llm",
  "scoreable_node_types": ["session"],
  "input_type": "sessions_trace_io_only",
  "output_type": "boolean",
  "defaults": {"model_name": "<model alias configured on your deployment>", "num_judges": 3}
}
```

`input_type` is the field that does the work here. `scoreable_node_types: ["session"]` is what makes
the metric session scoped, and if `input_type` is left out the server fills it in as
`sessions_normalized`, the full span detail form. `defaults` is required when `scorer_type` is `llm`.
The response carries the new metric's `id`.

**2. Add the judge prompt as a version.** `POST /scorers/{scorer_id}/version/llm`

```json
{"user_prompt": "Does the assistant maintain a professional brand tone across the whole conversation?"}
```

The server assembles the judge's full prompt from that instruction plus the metric's input type, and
returns it. Reading the assembled prompt is the fastest confirmation that the setting took effect.
With the per-turn setting it describes its input as a session object containing only the input and
output information from traces, without the detailed span information. Without the setting, the same
call yields a prompt that describes sessions, traces and each of the five span types.

**3. Enable it on the log stream.**
`PATCH /projects/{project_id}/runs/{log_stream_id}/scorer-settings`

```json
{"run_id": "<log_stream_id>",
 "scorers": [{"id": "<scorer_id>", "name": "brand-tone-and-language-quality"}]}
```

A log stream is the run in that path, so its id goes in the URL and in the body. Each entry needs the
metric's `id`; a name on its own is rejected. The call replaces the enabled set for that log stream,
so send every metric that should stay on.

Reading the result back:

- `GET /scorers/{scorer_id}` returns `input_type` and `default_version_id`.
- `GET /scorers/{scorer_id}/versions` returns each version with its assembled prompt.
- `GET /projects/{project_id}/runs/{log_stream_id}/scorer-settings` returns the enabled metrics and
  the version each is pinned to.

To resolve the ids, `POST /projects/paginated` with
`{"filters": [{"name": "name", "operator": "eq", "value": "<project name>"}]}`, then
`GET /projects/{project_id}/log_streams`.

Two things to know before building on this:

- `scoreable_node_types` cannot be changed after the metric exists. The API refuses it with "The
  field 'scoreable_node_types' cannot be changed after a scorer is created." Moving a metric between
  session scope and trace scope means creating a new metric.
- `input_type` can be changed later with `PATCH /scorers/{scorer_id}`, and that patch does not
  rewrite prompts that were already assembled. A version created before the change keeps its old
  prompt, so set the input type first and add the version after. Adding a version moves the pinned
  version on any log stream where the metric is already enabled, so no second enable call is needed.

## Files

```
turn_io_synthesis.py     the harness
fixtures/                the three OTLP payloads, committed; _build.py regenerates them
evidence/                rendered output from a verification run, so the numbers can be read
                         without running anything
.env.sample              the variables live mode needs
```

`evidence/inspect.txt` is the output of a current inspect run. In `evidence/live-run.txt` the s1
scenario description was edited before publication; every measured figure is as the run produced it.
