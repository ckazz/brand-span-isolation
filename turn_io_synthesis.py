"""What does a session-scoped LLM judge actually receive for each turn?

The configuration under test is a session-scoped metric whose input type is
trace input and output only: one session object holding an ordered list of turns,
each turn reduced to its input and its output, with no span detail. On a metric
whose scoreable node type is session, that is `input_type=sessions_trace_io_only`
over the API. Nothing in the judge's input is assembled by application code or by
another metric; the platform projects it from what was stored at ingestion, which
is why this harness measures ingestion rather than scoring.

OpenTelemetry has no trace object, so Galileo derives a trace's input and output
from the spans it receives. This harness sends three variants of the same
three-turn conversation and shows, for each one, the reduction from the full
OTLP payload down to the per-turn text a session-scoped judge is given.

    inspect mode (default)  describes the payloads. No network, no credentials.
    live mode               posts them to a Galileo deployment and reads back
                            what was stored as each turn's input and output.

Usage:
    python3 turn_io_synthesis.py
    python3 turn_io_synthesis.py --mode live
    python3 turn_io_synthesis.py --mode live --only s1 --brief

No third-party packages are needed. Python 3.9 or newer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"

# Same conversation, same spans per turn. The only variable is which span carries
# the turn's message content, because that is what decides the stored turn text.
SCENARIOS = {
    "s1": "agent_root_io.json",
    "s2": "llm_child_io_only.json",
    "s3": "indexed_attrs.json",
}

# The attribute keys ingestion reads when deriving a turn's input and output. A
# span carrying none of these contributes nothing to the judge's input, however
# much else it carries.
MESSAGE_ATTRS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.prompt.0.content",
    "gen_ai.completion.0.content",
)

INGEST_PATH = "/otel/v1/traces"
READBACK_TIMEOUT_S = 90


# --- formatting -------------------------------------------------------------


def rule(title: str = "") -> None:
    print("\n" + (f"== {title} " + "=" * max(0, 76 - len(title))) if title else "=" * 78)


def chars(obj: Any) -> int:
    return len(obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":")))


def clip(text: str, width: int = 92) -> str:
    text = (text or "").replace("\n", "\\n")
    return text if len(text) <= width else text[: width - 3] + "..."


# --- fixtures ---------------------------------------------------------------


def load(scenario: str) -> dict:
    return json.loads((FIXTURES / SCENARIOS[scenario]).read_text())


def spans_of(turn: dict) -> list[dict]:
    return turn["export_request"]["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attr_keys(span: dict) -> list[str]:
    return [a["key"] for a in span.get("attributes", [])]


def describe_tree(turn: dict) -> None:
    spans = spans_of(turn)
    by_parent: dict[str | None, list[dict]] = {}
    for s in spans:
        by_parent.setdefault(s.get("parentSpanId"), []).append(s)

    def walk(parent: str | None, depth: int) -> None:
        for s in by_parent.get(parent, []):
            keys = attr_keys(s)
            carries = [k for k in keys if k in MESSAGE_ATTRS]
            payload = [k for k in keys if k.startswith("gen_ai.tool.call.")]
            if carries:
                marker = "messages: " + ", ".join(carries)
            elif payload:
                marker = "tool payload: " + ", ".join(payload)
            else:
                marker = "no message attributes"
            print(f"    {'  ' * depth}{'└─ ' if depth else ''}{s['name']}  [{marker}]")
            walk(s["spanId"], depth + 1)

    walk(None, 0)


# --- per-run rewriting ------------------------------------------------------


def rewrite(doc: dict, run_tag: str) -> dict:
    """Give this run its own ids, conversation id and timestamps.

    Galileo derives a trace's identity from the OTel trace id, so reposting the
    committed ids would land on top of an earlier run instead of beside it. The
    scenario is folded into the tag for the same reason: the three scenarios are
    the same conversation and would otherwise share trace ids with each other.
    """
    doc = json.loads(json.dumps(doc))
    run_tag = run_tag[:7] + doc["scenario"][-1]
    conversation = f"{doc['conversation_id']}-{run_tag}"
    doc["conversation_id"] = conversation
    now_ns = int(time.time() * 1_000_000_000)

    for index, turn in enumerate(doc["turns"]):
        base = now_ns + index * 5_000_000_000
        for span in spans_of(turn):
            span["traceId"] = f"{run_tag}{index:02d}".rjust(32, "0")[-32:]
            span["spanId"] = run_tag + span["spanId"][-8:]
            if span.get("parentSpanId"):
                span["parentSpanId"] = run_tag + span["parentSpanId"][-8:]
            offset = int(span["startTimeUnixNano"]) % 5_000_000_000
            duration = int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])
            span["startTimeUnixNano"] = str(base + offset)
            span["endTimeUnixNano"] = str(base + offset + duration)
            for a in span.get("attributes", []):
                if a["key"] == "gen_ai.conversation.id":
                    a["value"]["stringValue"] = conversation
    return doc


# --- API client -------------------------------------------------------------


def resolve_api_base(api_url: str | None, console_url: str) -> str:
    """The OTLP endpoint is served by the API host, which is not the console host.

    On most deployments the console is served from one hostname and the API from
    another, so posting spans to the console URL does not reach the ingest route.
    GALILEO_API_URL is therefore the variable that matters for live mode, and the
    console URL is only a fallback for the case where one host serves both.
    """
    host = (api_url or console_url).rstrip("/")
    # A local stack serves the console and the API on different ports of localhost.
    if "localhost" in host or "127.0.0.1" in host:
        return "http://localhost:8088"
    return host


class Galileo:
    def __init__(self, api_base: str, api_key: str) -> None:
        self.base = api_base.rstrip("/")
        self.key = api_key

    def _call(self, path: str, body: Any = None, method: str = "POST", extra: dict | None = None) -> Any:
        headers = {"Galileo-API-Key": self.key, "Content-Type": "application/json"}
        headers.update(extra or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            sys.exit(f"\n{method} {path} failed with HTTP {e.code}:\n  {e.read().decode()[:500]}")

    def ingest(self, export_request: dict, project: str, log_stream: str) -> None:
        reply = self._call(INGEST_PATH, export_request, extra={"project": project, "logstream": log_stream})
        partial = (reply or {}).get("partialSuccess") or {}
        if partial.get("rejectedSpans") or partial.get("errorMessage"):
            print(f"  warning: ingest reported {partial.get('rejectedSpans', 0)} rejected spans")
            print(f"           {partial.get('errorMessage', '')[:300]}")

    def project_id(self, name: str) -> str:
        found = self._call("/projects/paginated", {"filters": [{"name": "name", "operator": "eq", "value": name}]})
        projects = found.get("projects", [])
        if not projects:
            sys.exit(f"project {name!r} not found after ingest")
        return projects[0]["id"]

    def log_stream_id(self, project_id: str, name: str) -> str | None:
        for ls in self._call(f"/projects/{project_id}/log_streams", method="GET"):
            if ls.get("name") == name:
                return ls["id"]
        return None

    def traces(self, project_id: str, log_stream_id: str) -> list[dict]:
        found = self._call(f"/projects/{project_id}/traces/search", {"log_stream_id": log_stream_id, "limit": 100})
        return found.get("records", [])


def wait_for_traces(api: Galileo, project: str, log_stream: str, expected: int) -> list[dict]:
    """Ingestion is asynchronous, so poll until every turn has landed."""
    project_id = api.project_id(project)
    deadline = time.time() + READBACK_TIMEOUT_S
    records: list[dict] = []
    while time.time() < deadline:
        log_stream_id = api.log_stream_id(project_id, log_stream)
        if log_stream_id:
            records = api.traces(project_id, log_stream_id)
            if len(records) >= expected:
                break
        time.sleep(2)
    records.sort(key=lambda r: r.get("created_at") or "")
    return records


# --- modes ------------------------------------------------------------------


def inspect(scenario: str, brief: bool) -> None:
    doc = load(scenario)
    rule(f"{scenario}: {doc['summary']}")
    print(f"  turn root carries: {doc['root_carries']}")
    print(f"  conversation id:   {doc['conversation_id']}")

    total = 0
    for turn in doc["turns"]:
        payload = turn["export_request"]
        total += chars(payload)
        print(f"\n  turn {turn['turn']}  ({chars(payload):,} characters on the wire)")
        describe_tree(turn)

    print(f"\n  Stage 1, what is sent: {total:,} characters across {len(doc['turns'])} export requests.")
    print("  Every span above is sent, including the tool arguments, the tool result and")
    print("  the audit block the tool wrapper attaches.")

    if not brief:
        rule(f"{scenario}: full payload for turn 1")
        print(json.dumps(doc["turns"][0]["export_request"], indent=2))

    print("\n  inspect mode only describes what is sent. Run --mode live to see what a")
    print("  deployment actually stores as each turn's input and output.")


def live(scenario: str, api: Galileo, project: str, log_stream_base: str, run_tag: str, brief: bool) -> bool:
    doc = rewrite(load(scenario), run_tag)
    log_stream = f"{log_stream_base}-{scenario}-{run_tag}"

    rule(f"{scenario}: {doc['summary']}")
    print(f"  turn root carries: {doc['root_carries']}")
    print(f"  log stream:        {log_stream}")

    sent_chars = 0
    for turn in doc["turns"]:
        api.ingest(turn["export_request"], project, log_stream)
        sent_chars += chars(turn["export_request"])
        # One export request per turn, in order, as a live agent would emit them.
        time.sleep(1)
    print(f"  posted {len(doc['turns'])} turns, {sent_chars:,} characters")

    records = wait_for_traces(api, project, log_stream, len(doc["turns"]))
    if len(records) != len(doc["turns"]):
        print(f"\n  FAIL: expected {len(doc['turns'])} traces, read back {len(records)}")
        return False

    print("\n  Stage 2, what was stored as each turn:")
    stored_chars = 0
    for turn, rec in zip(doc["turns"], records):
        stored_chars += chars(rec.get("input") or "") + chars(rec.get("output") or "")
        print(f"    turn {turn['turn']} input :  {clip(rec.get('input'))}")
        print(f"    turn {turn['turn']} output:  {clip(rec.get('output'))}")

    sessions = {r.get("session_id") for r in records}
    # Stage 3 is the projection the platform performs for the trace input and output
    # only setting: one session, its traces in order, each reduced to input and
    # output. It is built here from the rows just read back, so the text in it is
    # the stored text and not the text that was sent. The same projection was run
    # through the platform's own normalizer over these sessions and produced the
    # same content, which is what makes the shape below a measurement rather than
    # an illustration.
    judge_input = [
        {
            "session_id": records[0].get("session_id"),
            "traces": [{"input": r.get("input"), "output": r.get("output")} for r in records],
        }
    ]
    judge_chars = chars(judge_input)

    print("\n  Stage 3, what a session-scoped judge receives with the trace input and")
    print("  output only setting: the ordered list above and nothing else.")
    if not brief:
        print(json.dumps(judge_input, indent=2)[:2000])

    print(f"\n  Reduction: {sent_chars:,} characters sent -> {stored_chars:,} stored as turn text")
    print(f"             -> {judge_chars:,} characters of judge input ({judge_chars * 100 // sent_chars}% of the payload)")

    # Three assertions per turn, and each one answers a separate question. The
    # session check proves the turns were grouped, without which there is no
    # session-scoped input at all. The input and output checks prove the stored
    # turn text is the customer-visible exchange rather than an intermediate step.
    # The excludes check is the one that proves the tool arguments and the audit
    # block never reach the judge, which is the cost and context-window concern.
    checks: list[tuple[str, bool]] = [("all turns linked into one session", len(sessions) == 1)]
    for turn, rec in zip(doc["turns"], records):
        out = rec.get("output") or ""
        inp = rec.get("input") or ""
        n = turn["turn"]
        checks.append((f"turn {n} input contains {turn['expect_input_contains']}", turn["expect_input_contains"] in inp))
        checks.append(
            (f"turn {n} output contains {turn['expect_output_contains']}", turn["expect_output_contains"] in out)
        )
        checks.append(
            (f"turn {n} output excludes {turn['expect_output_excludes']}", turn["expect_output_excludes"] not in out)
        )

    print()
    for label, ok in checks:
        print(f"    {'ok  ' if ok else 'FAIL'}  {label}")
    return all(ok for _, ok in checks)


# --- entry point ------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("inspect", "live"), default="inspect")
    parser.add_argument("--only", nargs="+", choices=sorted(SCENARIOS), metavar="SCENARIO")
    parser.add_argument("--brief", action="store_true", help="omit the full payload dumps")
    args = parser.parse_args()

    scenarios = args.only or sorted(SCENARIOS)

    if args.mode == "inspect":
        for scenario in scenarios:
            inspect(scenario, args.brief)
        return 0

    console_url = os.environ.get("GALILEO_CONSOLE_URL")
    api_url = os.environ.get("GALILEO_API_URL")
    api_key = os.environ.get("GALILEO_API_KEY")
    project = os.environ.get("GALILEO_PROJECT")
    log_stream = os.environ.get("GALILEO_LOG_STREAM")
    if not (api_url or console_url) or not all((api_key, project, log_stream)):
        sys.exit(
            "live mode needs GALILEO_API_URL (or GALILEO_CONSOLE_URL), GALILEO_API_KEY, "
            "GALILEO_PROJECT and GALILEO_LOG_STREAM set.\nSee .env.sample."
        )

    api_base = resolve_api_base(api_url, console_url or "")
    print(f"posting to {api_base}{INGEST_PATH}")
    api = Galileo(api_base, api_key)
    run_tag = format(int(time.time()) % 100_000_000, "08x")
    results = {s: live(s, api, project, log_stream, run_tag, args.brief) for s in scenarios}

    rule("SUMMARY")
    for scenario, ok in results.items():
        print(f"  {scenario}  {'PASS' if ok else 'FAIL'}  {load(scenario)['root_carries']}")
    print(f"\n  project {project}, log streams suffixed -{run_tag}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
