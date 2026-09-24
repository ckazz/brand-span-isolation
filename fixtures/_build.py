"""Generate the three OTLP fixtures in this directory.

The fixtures are committed, so you only need this if you want to change the
conversation text or the span shape. Run it from the repo root:

    python3 fixtures/_build.py

Every scenario is the same three-turn conversation with the same four spans per
turn. The only thing that varies is where the turn's message attributes sit.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent

CONVERSATION_ID = "brand-span-isolation-conversation"
MODEL = "gpt-4o-mini"

# Sentinels are deliberately ugly so a wrong answer is obvious on sight rather
# than needing a diff.
TURNS = [
    {
        "user": "USER-T1 What is my current balance?",
        "reply": "REPLY-T1 Your balance is $11,111.11.",
        "tool_arg": "AX-ACCOUNT-T1",
        "tool_result": "balance=11111.11 currency=USD",
    },
    {
        "user": "USER-T2 And when was my last payment?",
        "reply": "REPLY-T2 Your last payment was $2,222.22 on 3 March.",
        "tool_arg": "AX-ACCOUNT-T2",
        "tool_result": "last_payment=2222.22 date=2026-03-03",
    },
    {
        "user": "USER-T3 Please email me the statement.",
        "reply": "REPLY-T3 Sent, check your inbox shortly.",
        "tool_arg": "AX-ACCOUNT-T3",
        "tool_result": "statement_email=queued ref=ST-99812",
    },
]

# The text that a tool-calling step leaves as an LLM span's output. This string
# is ours, not Galileo's: nothing in the platform labels intermediate steps.
INTERMEDIATE = "INTERMEDIATE-TOOLCALL-STEP calling lookup_account"


def audit_blob(turn: int, tool_result: str) -> str:
    """The kind of payload a compliance-instrumented tool span tends to carry."""
    return json.dumps(
        {
            "result": tool_result,
            "audit": {
                "request_id": f"req-{turn:04d}-8f2ac1de-{turn}",
                "caller_identity": "svc-brand-assistant@example.internal",
                "policy_evaluations": [
                    {"policy": "pci.account_read", "decision": "allow", "latency_ms": 4},
                    {"policy": "pii.egress_scan", "decision": "allow", "matches": 0, "latency_ms": 11},
                    {"policy": "geo.residency", "decision": "allow", "region": "us-east-1", "latency_ms": 2},
                ],
                "downstream_calls": [
                    {"system": "core-ledger", "op": "GET /accounts/{ref}/balance", "status": 200, "ms": 38},
                    {"system": "fraud-signals", "op": "POST /score", "status": 200, "ms": 22},
                ],
                "retention": {"class": "operational", "ttl_days": 400, "legal_hold": False},
                "trace_flags": {"sampled": True, "debug": False},
                "notes": "Emitted by the tool wrapper for every invocation regardless of outcome.",
            },
        },
        indent=None,
    )


def messages(role: str, text: str) -> str:
    return json.dumps([{"role": role, "parts": [{"type": "text", "content": text}]}])


def attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def span(
    *,
    name: str,
    span_id: str,
    trace_id: str,
    parent: str | None,
    start_ns: int,
    end_ns: int,
    attributes: list[dict],
) -> dict:
    s = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes,
    }
    if parent:
        s["parentSpanId"] = parent
    return s


def build_turn(scenario: str, index: int, turn: dict) -> dict:
    """One export request: the four spans of a single conversation turn."""
    n = index + 1
    trace_id = f"{n:02d}" * 16
    base = 1789600000000000000 + index * 10_000_000_000

    root_attrs = [
        attr("gen_ai.operation.name", "invoke_agent"),
        attr("gen_ai.agent.name", "brand-assistant"),
        attr("gen_ai.conversation.id", CONVERSATION_ID),
    ]
    if scenario == "agent_root_io":
        root_attrs += [
            attr("gen_ai.input.messages", messages("user", turn["user"])),
            attr("gen_ai.output.messages", messages("assistant", turn["reply"])),
        ]
    elif scenario == "indexed_attrs":
        root_attrs += [
            attr("gen_ai.prompt.0.role", "user"),
            attr("gen_ai.prompt.0.content", turn["user"]),
            attr("gen_ai.completion.0.role", "assistant"),
            attr("gen_ai.completion.0.content", turn["reply"]),
        ]
    elif scenario != "llm_child_io_only":
        raise ValueError(scenario)

    spans = [
        span(
            name="invoke_agent brand-assistant",
            span_id=f"{n:02d}a1" * 4,
            trace_id=trace_id,
            parent=None,
            start_ns=base,
            end_ns=base + 4_000_000_000,
            attributes=root_attrs,
        ),
        # First LLM step: decides to call a tool, so its only output is the call.
        span(
            name=f"chat {MODEL}",
            span_id=f"{n:02d}b2" * 4,
            trace_id=trace_id,
            parent=f"{n:02d}a1" * 4,
            start_ns=base + 100_000_000,
            end_ns=base + 900_000_000,
            attributes=[
                attr("gen_ai.operation.name", "chat"),
                attr("gen_ai.request.model", MODEL),
                attr("gen_ai.input.messages", messages("user", turn["user"])),
                attr("gen_ai.output.messages", messages("assistant", INTERMEDIATE)),
            ],
        ),
        span(
            name="execute_tool lookup_account",
            span_id=f"{n:02d}c3" * 4,
            trace_id=trace_id,
            parent=f"{n:02d}a1" * 4,
            start_ns=base + 1_000_000_000,
            end_ns=base + 2_000_000_000,
            attributes=[
                attr("gen_ai.operation.name", "execute_tool"),
                attr("gen_ai.tool.name", "lookup_account"),
                attr("gen_ai.tool.call.arguments", json.dumps({"account_ref": turn["tool_arg"]})),
                attr("gen_ai.tool.call.result", audit_blob(n, turn["tool_result"])),
            ],
        ),
        # Second LLM step: the one that actually produces the customer-visible reply.
        span(
            name=f"chat {MODEL}",
            span_id=f"{n:02d}d4" * 4,
            trace_id=trace_id,
            parent=f"{n:02d}a1" * 4,
            start_ns=base + 2_100_000_000,
            end_ns=base + 3_800_000_000,
            attributes=[
                attr("gen_ai.operation.name", "chat"),
                attr("gen_ai.request.model", MODEL),
                attr("gen_ai.input.messages", messages("tool", turn["tool_result"])),
                attr("gen_ai.output.messages", messages("assistant", turn["reply"])),
            ],
        ),
    ]

    return {
        "turn": n,
        "user": turn["user"],
        "reply": turn["reply"],
        "export_request": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            attr("service.name", "brand-assistant"),
                            attr("deployment.environment", "verification"),
                        ]
                    },
                    "scopeSpans": [{"scope": {"name": "vendor.otel"}, "spans": spans}],
                }
            ]
        },
    }


SCENARIOS = {
    "s1": {
        "file": "agent_root_io.json",
        "shape": "agent_root_io",
        "root_carries": "gen_ai.input.messages and gen_ai.output.messages",
        "summary": "Turn root carries the turn's own messages. This is the shape currently reported as being sent.",
        "expect_reply_as_turn_output": True,
    },
    "s2": {
        "file": "llm_child_io_only.json",
        "shape": "llm_child_io_only",
        "root_carries": "no message attributes at all",
        "summary": "Turn root carries no messages, so only the nested LLM and tool spans have content.",
        "expect_reply_as_turn_output": False,
    },
    "s3": {
        "file": "indexed_attrs.json",
        "shape": "indexed_attrs",
        "root_carries": "indexed gen_ai.prompt.N and gen_ai.completion.N",
        "summary": "Turn root carries its messages in the older indexed attribute style.",
        "expect_reply_as_turn_output": True,
    },
}


def main() -> None:
    for sid, meta in SCENARIOS.items():
        turns = [build_turn(meta["shape"], i, t) for i, t in enumerate(TURNS)]
        for t in turns:
            if meta["expect_reply_as_turn_output"]:
                t["expect_output_contains"] = t["reply"].split(" ")[0]
                t["expect_output_excludes"] = "INTERMEDIATE-TOOLCALL-STEP"
            else:
                t["expect_output_contains"] = "INTERMEDIATE-TOOLCALL-STEP"
                t["expect_output_excludes"] = t["reply"].split(" ")[0]
            t["expect_input_contains"] = t["user"].split(" ")[0]
        doc = {
            "scenario": sid,
            "conversation_id": CONVERSATION_ID,
            "root_carries": meta["root_carries"],
            "summary": meta["summary"],
            "turns": turns,
        }
        out = HERE / meta["file"]
        out.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"wrote {out.name}  ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
