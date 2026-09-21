"""Synthetic transcript generator, gold resolver, and baselines for the E5 decision gate.

Every session in the corpus is written from templates and a seeded RNG, satisfying
the constraint that no real machine history or owner transcripts are read or adapted.

The scorer lives in `bench/gate.py`, not here, so that an extractor author can be
given the scoring without being given the templates. [E5]
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bench.gate import get_decision_slice, post_failure_blocks, score_predictions, score_with_slices
from gitmemory import adapters
from gitmemory.records import Session

__all__ = [  # re-exported: the tests and the baselines import the scorer from here
    "get_decision_slice",
    "score_predictions",
]

# Disjoint vocabularies for dev and test splits to prevent data leakage and
# ensure diverse surface forms.
DEV_VARS: dict[str, list[str]] = {
    "library": ["xml", "msgpack", "protobuf", "ini", "plist", "parquet"],
    "feature": [
        "rate limiting",
        "throttling",
        "input parsing",
        "payload hashing",
        "header injection",
        "error tracking",
    ],
    "pattern": [
        "facade pattern",
        "adapter wrap",
        "singleton pattern",
        "observer registry",
        "strategy dispatch",
    ],
    "avoid": [
        "multiple inheritance",
        "mutable defaults",
        "eval execution",
        "untyped casts",
        "arbitrary imports",
    ],
    "bad_library": ["httpx", "pyyaml", "pydantic", "aiohttp", "django", "jinja2"],
    "good_library": [
        "http.client",
        "configparser",
        "dataclasses",
        "asyncio",
        "socketserver",
        "string.Template",
    ],
    "data": [
        "access tokens",
        "request logs",
        "api payloads",
        "heartbeat pings",
        "process state",
        "metrics metadata",
    ],
    "format_a": [
        "raw bytes",
        "UTF-8 text",
        "hexadecimal string",
        "base64 stream",
        "msgpack structure",
    ],
    "format_b": [
        "pickled objects",
        "HTML table",
        "unstructured CSV",
        "nested XML",
        "global list state",
    ],
    "task": [
        "api requests",
        "config loading",
        "data structures",
        "process execution",
        "socket binds",
        "string formatting",
    ],
    "component": ["listener", "handler", "formatter", "worker", "supervisor", "aggregator"],
    "pattern_a": [
        "multiprocessing",
        "iterative generators",
        "queue consumers",
        "signal trap",
        "coroutine wait",
    ],
    "pattern_b": [
        "greenlets",
        "infinite recursion",
        "busy wait loop",
        "blocking locks",
        "raw sockets",
    ],
    "old_approach": [
        "unbuffered write",
        "glob search loop",
        "subprocess shell call",
        "synchronous socket fetch",
        "file-based locking",
    ],
    "new_approach": [
        "memory-mapped file",
        "iterative walk",
        "direct execve call",
        "asyncio streams",
        "redis distributed lock",
    ],
    "reason": [
        "unbuffered writes cause disk thrashing",
        "glob loops miss hidden dotfiles",
        "shell calls expose injection risks",
        "sync fetch blocks the event loop",
        "file locks can deadlock on crash",
    ],
    "module": ["stream handler", "query builder", "auth helper", "job dispatcher", "event queue"],
    "issue": [
        "socket exhaustion",
        "memory leaks",
        "deadlocks",
        "busy-waiting overhead",
        "unhandled exceptions",
    ],
    "opt_a": ["Elasticsearch", "MySQL", "Cassandra", "InfluxDB"],
    "opt_b": ["OpenSearch", "MariaDB", "ScyllaDB", "Prometheus"],
    "pros_a": [
        "highly optimized for search",
        "fully transactional",
        "excellent write throughput",
        "native time-series support",
    ],
    "pros_b": [
        "drop-in replacement",
        "widely supported",
        "low resource foot print",
        "flexible dimensional model",
    ],
    "task_desc": ["the payload validation", "the rate limit check", "the webhook delivery"],
    "plan_a": [
        "subclass the validation middleware",
        "integrate third-party ratelimiting",
        "embed raw shell scripts",
    ],
    "plan_b": [
        "write a custom decorator",
        "use token bucket algorithm",
        "poll the webhook endpoint",
    ],
    "plan_c": [
        "raise ValidationError directly",
        "use sliding window logs",
        "expose an HTTP callback",
    ],
    "old_narrative": ["SQLite back-end", "direct TCP socket", "plain CSV storage"],
    "new_narrative": ["Postgres back-end", "TLS secure channel", "structured JSON storage"],
    "file_name": ["helper.py", "store.py", "runner.py"],
    "bad_syntax": ["definiton", "excec", "initilize"],
    "good_syntax": ["definition", "exec", "initialize"],
    "retry_status": [
        "That failed",
        "The command failed",
        "The verification failed",
        "Since it failed",
        "It failed",
    ],
    "retry_action": [
        "run the command",
        "execute the check",
        "run the script",
        "trigger the diagnostic",
        "run the validation suite",
        "evaluate the output",
    ],
    "retry_reason": [
        "verify the output",
        "check the result",
        "verify the state",
        "inspect the error",
        "verify the outcome",
        "see what happened",
    ],
}

TEST_VARS: dict[str, list[str]] = {
    "lock_type": [
        "read-write lock",
        "distributed lock",
        "atomic flag",
        "counting semaphore",
        "named pipe lock",
    ],
    "resource": [
        "shared memory buffer",
        "network socket",
        "transaction log",
        "audit trail file",
        "active task map",
    ],
    "unstable_lock": [
        "volatile boolean",
        "unsynchronized counter",
        "file existence test",
        "thread sleep delay",
        "pid check file",
    ],
    "output": [
        "telemetry metrics",
        "error diagnostics",
        "configuration schema",
        "conformance log",
        "billing record",
    ],
    "format_good": [
        "msgpack objects",
        "gzipped JSON",
        "protobuf stream",
        "ISO-8601 timestamps",
        "base64 encoding",
    ],
    "format_bad": [
        "nested tables",
        "raw repr string",
        "plain comma list",
        "undocumented XML",
        "unstructured text",
    ],
    "service": ["exporter", "forwarder", "orchestrator", "watchdog"],
    "dest_good": [
        "syslog facility",
        "systemd journal",
        "local rotating file",
        "stderr stream",
        "dedicated UDP socket",
    ],
    "dest_bad": [
        "stdout stream",
        "shared memory block",
        "global terminal",
        "unencrypted pipe",
        "public URL",
    ],
    "item": ["secrets key", "session payload", "access token", "database password", "user context"],
    "container_good": [
        "os.environ map",
        "encrypted vault",
        "temp directory",
        "private object property",
        "secure cookie",
    ],
    "container_bad": [
        "public static field",
        "tracked config file",
        "current directory",
        "unencrypted global",
        "session dictionary",
    ],
    "old_tech": [
        "the synchronous fetch approach",
        "the temporary database",
        "thread pool executors",
        "shared dictionary objects",
        "global array buffers",
    ],
    "new_tech": [
        "an asynchronous queue",
        "a Redis cache store",
        "multiprocessing workers",
        "immutable state structures",
        "ring-buffer segments",
    ],
    "reason_test": [
        "synchronous fetches freeze the loop",
        "temporary databases leak on exit",
        "thread pools cause race conditions",
        "shared dicts suffer from lock contention",
        "global arrays trigger size overflows",
    ],
    "target": [
        "the alert forwarder",
        "the payload parser",
        "the metrics collector",
        "the task controller",
        "the route dispatcher",
    ],
    "bug": [
        "deadlock conditions",
        "connection drops",
        "memory exhaustion",
        "data race errors",
        "silent file truncations",
    ],
    "opt_a": ["Kubernetes", "Prometheus", "Envoy", "Terraform"],
    "opt_b": ["Nomad", "Grafana", "HAProxy", "OpenTofu"],
    "pros_a": [
        "orchestrates at scale",
        "native scraping query support",
        "highly performant proxying",
        "declarative state management",
    ],
    "pros_b": [
        "lightweight scheduling",
        "beautiful visualization",
        "stable battle-tested features",
        "fully open source",
    ],
    "task_desc": ["the ingestion pipeline", "the query routing", "the config distribution"],
    "plan_a": ["deploy a Kafka cluster", "write custom SQL joins", "re-architect with systemd"],
    "plan_b": ["use Redis streams", "use NoSQL document query", "re-architect with launchd"],
    "plan_c": [
        "use zero-mq sockets",
        "query file system directly",
        "re-architect with container entry",
    ],
    "old_narrative": ["git submodules", "cron-based tasks", "monolithic architecture"],
    "new_narrative": ["cargo dependencies", "event-driven queues", "microservice architecture"],
    "file_name": ["client.py", "auth.py", "scheduler.py"],
    "bad_syntax": ["receve", "authen", "sheduler"],
    "good_syntax": ["receive", "authenticate", "scheduler"],
    "retry_status_test": [
        "The check failed",
        "That command failed",
        "The runner failed",
        "Since that failed",
        "The step failed",
    ],
    "retry_action_test": [
        "launch the command",
        "run that verification",
        "re-run the runner",
        "run the same runner",
        "trigger the test",
        "execute the validation",
    ],
    "retry_reason_test": [
        "inspect the log",
        "check the log",
        "verify the result",
        "check the outcome",
        "inspect the traceback",
        "verify the response",
    ],
}

DEV_DIRECTIVES_TEMPLATES: list[str] = [
    "Ensure {library} is used for input parsing; avoid hand-rolled patterns.",
    "Avoid hand-rolled parsing patterns. Use {library} instead.",
    "Let's enforce {feature} via {pattern} to prevent {avoid}.",
    "Do not allow {avoid} in {feature}; deploy {pattern} for this purpose.",
    "Make sure we do not use {bad_library} for {task}. Rely on standard {good_library}.",
    "We must use {good_library} for {task} rather than {bad_library}.",
    "Store {data} in {format_a} instead of {format_b}.",
    "Avoid storing {data} in {format_b}; serialize to {format_a} instead.",
    "Never configure the {component} with {pattern_b}; we must use {pattern_a}.",
    "The {component} needs {pattern_a} and must never deploy {pattern_b}.",
]

DEV_REVERSALS_TEMPLATES: list[str] = [
    "On second thought, let's swap {old_approach} with {new_approach} because {reason}.",
    "I will pivot now: I am replacing {old_approach} with {new_approach} since {reason}.",
    "Let's refactor {module} to use {new_approach} instead of {old_approach} to prevent {issue}.",
    (
        "I have decided to stop using {old_approach} for the {module}. "
        "I will use {new_approach} because of {issue}."
    ),
    (
        "Let's alter our course: discarding {old_approach} "
        "and adopting {new_approach} to resolve {issue}."
    ),
    (
        "I am going to move away from {old_approach} in {module}. "
        "Instead, I will leverage {new_approach} as it mitigates {issue} better."
    ),
]

TEST_DIRECTIVES_TEMPLATES: list[str] = [
    "Employ a {lock_type} to guard the {resource} instead of {unstable_lock}.",
    "Avoid using {unstable_lock}; use {lock_type} to protect the {resource}.",
    "We should write {output} in {format_good} rather than {format_bad}.",
    "Do not export {output} in {format_bad}. Ensure it uses {format_good}.",
    "Make the {service} write to {dest_good} rather than {dest_bad}.",
    "Avoid sending {service} output to {dest_bad}; it must go to {dest_good}.",
    "Store the {item} inside a {container_good} instead of {container_bad}.",
    "Do not rely on {container_bad} for {item}. Always use {container_good}.",
]

TEST_REVERSALS_TEMPLATES: list[str] = [
    "We need to redirect our efforts: swapping {old_tech} with {new_tech} as {reason_test}.",
    "Actually, I am abandoning {old_tech} in favor of {new_tech} because {reason_test}.",
    "I will stop deploying {old_tech} for {target} and integrate {new_tech} to eliminate {bug}.",
    (
        "Let's change our implementation: discarding {old_tech} "
        "and utilizing {new_tech} which resolves {bug}."
    ),
    "I am replacing {old_tech} with {new_tech} in {target} to fix the {bug}.",
    "Let's redirect: I will abandon {old_tech} and set up {new_tech} to circumvent {bug}.",
]

DEV_NO_CHOICE: list[str] = [
    "Please make sure the indentation is uniform.",
    "Can we look into the rate limiting issue?",
    "Confirm that the test runner finishes without warnings.",
    "We should remove old log files from the directory.",
    "Make sure the code matches the formatting guidelines.",
]

TEST_NO_CHOICE: list[str] = [
    "Can we minimize the memory usage of this process?",
    "Please introduce detailed trace logs for the service.",
    "Verify that the active task map is cleaned up.",
    "We should write API specifications for these actions.",
    "Ensure the handler handles timeout exceptions correctly.",
]

DEV_PLAN: list[str] = [
    "I will check the active repository path to locate helper.py.",
    "Next, I will run the code style linter.",
    "I am going to print the contents of helper.py to find the line.",
    "Let's write a small script to verify this behavior.",
    "I will examine the git diff of the repository.",
]

TEST_PLAN: list[str] = [
    "I will open a connection to the socket to check if it's open.",
    "Next, I will read the trace logs to inspect the traceback.",
    "Let's benchmark the performance of the task supervisor.",
    "I will create a fresh git branch for the refactored code.",
    "I will check the running processes to see if the forwarder is active.",
]

DEV_RESPONSES: list[str] = [
    "I have verified the file contents and they look good.",
    "Let's proceed with the next step of our plan.",
    "The validation logic is now ready.",
    "I am updating the local files with these edits.",
]

TEST_RESPONSES: list[str] = [
    "I have checked the socket connection and it is established.",
    "Let's move on to running the integration tests.",
    "The authentication helper has been successfully configured.",
    "I will update the main runner script with the latest version.",
]

DEV_PROMPTS: list[str] = [
    "Can you show me the files in this directory?",
    "Let's run the style check first.",
    "What should we do next?",
    "How does the local repository status look?",
]

TEST_PROMPTS: list[str] = [
    "Can you check the socket state?",
    "Let's run the functional tests.",
    "What is the next item on the list?",
    "How does the supervisor status look?",
]

DEV_OPENING_TEXTS: list[str] = [
    "Let's run the main test suite to check our progress.",
    "I will execute the validation script to verify the change.",
    "Let's first run the validation checks to make sure.",
    "I should run the check script directly to verify.",
    "Let's run the diagnostic check first to see if it works.",
    "I will trigger the local check to verify the state.",
    "Let's execute the tests to check the current behavior.",
    "I will run the command to verify the file status.",
]

TEST_OPENING_TEXTS: list[str] = [
    "Let's run the test runner to inspect the state.",
    "I will launch the verification command to check.",
    "Let's run the active verification script to verify.",
    "I should execute the task test suite to make sure.",
    "Let's run the diagnostic suite first to verify progress.",
    "I will trigger the verification runner to see.",
    "Let's execute the validation runner to test the setup.",
    "I will run the check command to inspect the output.",
]

DEV_COMMANDS: list[str] = [
    "python -m pytest -v tests/test_gitrepo.py",
    "python -m pytest -v tests/test_store.py",
    "python -m pytest -v tests/test_daemon.py",
    "python -m pytest -v tests/test_index.py",
    "python -m pytest -v tests/test_hook.py",
    "python -m pytest -v tests/test_derive.py",
    "python -m pytest -v tests/test_conformance.py",
    "python -m pytest -v tests/test_no_owner_data.py",
]

TEST_COMMANDS: list[str] = [
    "npm run test:unit",
    "node check_deps.js",
    "node run_validation.js",
    "npm run lint:check",
    "npm run test:conformance",
    "node scripts/test_socket.js",
    "npm run test:e2e",
    "node build_check.js",
]

DEV_TOOL_ERRORS: list[str] = [
    "Exit code 1\nAssertionError: test_connection failed to connect to local daemon.",
    "Exit code 1\nFileNotFoundError: [Errno 2] No such file or directory: 'config.json'",
    "Exit code 1\nImportError: cannot import name 'get_git_repo' from 'gitmemory'",
    "Exit code 1\nPermissionError: [Errno 13] Permission denied: '/var/log/audit.log'",
    "Exit code 1\nAttributeError: 'NoneType' object has no attribute 'get_records'",
    "Exit code 1\nConnectionRefusedError: [Errno 61] Connection refused: localhost:8080",
]

TEST_TOOL_ERRORS: list[str] = [
    "Exit code 1\nError: Cannot find module './config.json'\nRequire stack:",
    "Exit code 1\nAssertionError [ERR_ASSERTION]: Expected true but got false",
    "Exit code 1\nTypeError: Cannot read properties of undefined (reading 'connect')",
    "Exit code 1\nError: listen EADDRINUSE: address already in use :::3000",
    "Exit code 1\nError: EACCES: permission denied, open '/var/run/app.pid'",
    "Exit code 1\nError: Connection timed out after 5000ms at Socket.connect",
]

DEV_RETRY_PROSE: list[str] = [
    "{retry_status}, let me {retry_action} again to {retry_reason}.",
    "Let me try {retry_action} once more to {retry_reason}.",
    "{retry_status}. I will re-{retry_action} to {retry_reason}.",
    "Let's {retry_action} again to {retry_reason}.",
    "I will {retry_action} once more to {retry_reason}.",
    "{retry_status}, let's re-{retry_action} to {retry_reason}.",
]

TEST_RETRY_PROSE: list[str] = [
    "{retry_status_test}, let me {retry_action_test} again to {retry_reason_test}.",
    "Let me try {retry_action_test} once more to {retry_reason_test}.",
    "{retry_status_test}. I will re-{retry_action_test} to {retry_reason_test}.",
    "Let's {retry_action_test} again to {retry_reason_test}.",
    "I will {retry_action_test} once more to {retry_reason_test}.",
    "{retry_status_test}, let's re-{retry_action_test} to {retry_reason_test}.",
]

DEV_SUCCESS_MESSAGES: list[str] = [
    "Exit code 0\nRan 12 tests. All passed successfully.\n",
    "Exit code 0\n15 passed in 0.05 seconds.\n",
    "Exit code 0\nPytest execution completed with zero failures.\n",
    "Exit code 0\nEverything matches in our verification check.",
    "Exit code 0\nRan 8 checks. Status: OK.\n",
    "Exit code 0\nAll unit tests succeeded.\n",
]

TEST_SUCCESS_MESSAGES: list[str] = [
    "Exit code 0\nDone. 8 specs. No bugs.\n",
    "Exit code 0\nValidation finished. Found 0 errors.",
    "Exit code 0\nActive processes stopped without problem.\n",
    "Exit code 0\nLocal suites finalized. Code is clear.\n",
    "Exit code 0\nTasks finished. Result: clean.\n",
    "Exit code 0\nPipeline finalized. Outputs generated correctly.\n",
]


@dataclass(frozen=True)
class Case:
    transcript_bytes: bytes
    # Recorded as a list of (uuid, block index, kind)
    planted_decisions: list[tuple[str, int, str]]


@dataclass(frozen=True, slots=True)
class Decision:
    kind: str  # "directive" | "reversal"
    source_ref: str  # a Block.block_id belonging to this session


def deterministic_uuid(content: str) -> str:
    """Generate a deterministic UUID string based on the given content string."""
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def generate_assistant_usage(rng: random.Random) -> dict[str, int]:
    """Draw input and output tokens uniformly to avoid any metadata leak."""
    return {
        "input_tokens": rng.randint(100, 500),
        "output_tokens": rng.randint(50, 200),
    }


def format_template(rng: random.Random, split: str, template: str) -> str:
    """Format a template by selecting random values from disjoint split vocabularies."""
    vars_dict = DEV_VARS if split == "dev" else TEST_VARS
    kwargs = {}
    for key, values in vars_dict.items():
        if f"{{{key}}}" in template:
            kwargs[key] = rng.choice(values)
    return template.format(**kwargs)


def generate_session(rng: random.Random, session_idx: int, split: str) -> Case:
    """Generate a single realistic session containing 20-80 turns.

    Includes planted decisions and distractors.
    """
    num_turns = rng.randint(20, 80)
    session_id = f"s{session_idx}"
    timestamp = 1789999000  # Seeded epoch timestamp

    # B5: vary gold per session, including 0
    if session_idx % 6 == 0:
        target_directives = 0
        target_reversals = 0
    else:
        # Vary between 1 and 3, averaging 2.2 for each
        target_directives = rng.choice([1, 2, 2, 3, 3])
        target_reversals = rng.choice([1, 2, 2, 3, 3])

    directives_planted = 0
    reversals_planted = 0

    planted_directives_data: list[str] = []
    line_objs: list[dict[str, Any]] = []
    planted_decisions: list[tuple[str, int, str]] = []
    last_uuid: str | None = None
    pending_turns: list[dict[str, Any]] = []

    t_idx = 0
    while t_idx < num_turns or pending_turns:
        # If there are queued turns from multi-turn sequence distractors, output them
        if pending_turns:
            turn_obj = pending_turns.pop(0)
            turn_uuid = deterministic_uuid(f"session_{session_idx}_turn_{t_idx}")
            turn_obj["uuid"] = turn_uuid
            turn_obj["sessionId"] = session_id

            # Format timestamp as ISO 8601
            from datetime import UTC, datetime

            dt = datetime.fromtimestamp(timestamp, tz=UTC)
            turn_obj["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            turn_obj["parentUuid"] = last_uuid

            line_objs.append(turn_obj)
            last_uuid = turn_uuid
            timestamp += rng.randint(2, 10)
            t_idx += 1
            continue

        role = "user" if t_idx % 2 == 0 else "assistant"
        turn_uuid = deterministic_uuid(f"session_{session_idx}_turn_{t_idx}")

        from datetime import UTC, datetime

        dt = datetime.fromtimestamp(timestamp, tz=UTC)
        ts_str = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        turn_obj = {
            "type": role,
            "uuid": turn_uuid,
            "sessionId": session_id,
            "timestamp": ts_str,
            "parentUuid": last_uuid,
            "cwd": "/workspace/dev-env" if split == "dev" else "/workspace/test-env",
            "version": "1.1.0" if split == "dev" else "1.2.0",
        }

        if role == "user":
            remaining_user_turns = (num_turns - t_idx + 1) // 2
            need_to_plant_directives = target_directives - directives_planted

            # Plant gold directive
            if need_to_plant_directives > 0 and (
                rng.random() < 0.25 or remaining_user_turns <= need_to_plant_directives
            ):
                tpl = rng.choice(
                    DEV_DIRECTIVES_TEMPLATES if split == "dev" else TEST_DIRECTIVES_TEMPLATES
                )
                text = format_template(rng, split, tpl)
                planted_directives_data.append(text)

                # B6 & B7: Some gold directives sit at block index >= 1 in a multi-block user turn
                if rng.random() < 0.30:
                    user_header = (
                        "Got it. Here is the primary guideline to apply:"
                        if split == "dev"
                        else "Understood. Let's make sure we implement this instruction:"
                    )
                    turn_obj["message"] = {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": user_header,
                            },
                            {"type": "text", "text": text},
                        ],
                    }
                    planted_decisions.append((turn_uuid, 1, "directive"))
                else:
                    if rng.random() < 0.5:
                        turn_obj["message"] = {
                            "role": "user",
                            "content": [{"type": "text", "text": text}],
                        }
                    else:
                        turn_obj["message"] = {"role": "user", "content": text}
                    planted_decisions.append((turn_uuid, 0, "directive"))
                directives_planted += 1

            elif planted_directives_data and rng.random() < 0.25:
                # Distractor: Restated user directive (only first instance is gold!)
                original_text = rng.choice(planted_directives_data)
                dev_prefixes = [
                    "Per my earlier point, ",
                    "As previously stated, ",
                    "To recall, ",
                    "As noted before, ",
                ]
                test_prefixes = [
                    "As specified earlier, ",
                    "Please recall that, ",
                    "To repeat, ",
                    "As I already requested, ",
                ]
                prefixes = dev_prefixes if split == "dev" else test_prefixes
                text = rng.choice(prefixes) + original_text
                if rng.random() < 0.20:
                    reminder_hdr = (
                        "A quick note regarding our configuration."
                        if split == "dev"
                        else "Please keep this instruction in mind."
                    )
                    turn_obj["message"] = {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": reminder_hdr},
                            {"type": "text", "text": text},
                        ],
                    }
                else:
                    turn_obj["message"] = {"role": "user", "content": text}

            elif rng.random() < 0.35:
                # Distractor: User comparing options choosing neither
                if split == "dev":
                    tpl = (
                        "We are choosing between {opt_a} and {opt_b}; "
                        "note that {opt_a} is {pros_a} and {opt_b} is {pros_b}."
                    )
                else:
                    tpl = (
                        "We could deploy either {opt_a} or {opt_b}, where "
                        "{opt_a} offers {pros_a} but {opt_b} provides {pros_b}."
                    )
                text = format_template(rng, split, tpl)
                if rng.random() < 0.20:
                    discuss_hdr = (
                        "We should evaluate the potential approaches."
                        if split == "dev"
                        else "Let's look at the available options before starting."
                    )
                    turn_obj["message"] = {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": discuss_hdr},
                            {"type": "text", "text": text},
                        ],
                    }
                else:
                    turn_obj["message"] = {"role": "user", "content": text}

            elif rng.random() < 0.5:
                # Distractor: No-choice user constraint (simple recommendation)
                text = rng.choice(DEV_NO_CHOICE if split == "dev" else TEST_NO_CHOICE)
                if rng.random() < 0.20:
                    note_hdr = (
                        "Also, keep in mind:" if split == "dev" else "Here is an extra detail:"
                    )
                    turn_obj["message"] = {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": note_hdr},
                            {"type": "text", "text": text},
                        ],
                    }
                else:
                    turn_obj["message"] = {"role": "user", "content": text}

            else:
                # Standard user prompt
                prompts = DEV_PROMPTS if split == "dev" else TEST_PROMPTS
                text = rng.choice(prompts)
                if rng.random() < 0.20:
                    moving_hdr = "Let's proceed." if split == "dev" else "Moving to the next step."
                    turn_obj["message"] = {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": moving_hdr},
                            {"type": "text", "text": text},
                        ],
                    }
                else:
                    turn_obj["message"] = {"role": "user", "content": text}

        else:
            remaining_assistant_turns = (num_turns - t_idx) // 2
            need_to_plant_reversals = target_reversals - reversals_planted

            # B1: Ninth family - genuine tool failure followed by genuine gold reversal
            # C4: Tuned from 0.40 to 0.18 to target 40%-60% of gold reversals post-failure
            if need_to_plant_reversals > 0 and (num_turns - t_idx) >= 4 and rng.random() < 0.18:
                tool_use_id = f"tu_{session_idx}_{t_idx}"
                cmd_pool = DEV_COMMANDS if split == "dev" else TEST_COMMANDS
                cmd = rng.choice(cmd_pool)
                opening_pool = DEV_OPENING_TEXTS if split == "dev" else TEST_OPENING_TEXTS
                text = rng.choice(opening_pool)
                tool_name = "bash" if split == "dev" else "sh"
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": text},
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": tool_name,
                            "input": {"command": cmd},
                        },
                    ],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }

                # Queue next 3 turns to complete the sequence
                error_pool = DEV_TOOL_ERRORS if split == "dev" else TEST_TOOL_ERRORS
                err_content = rng.choice(error_pool)
                tool_result_1 = {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": err_content,
                            }
                        ],
                    },
                }

                # Turn 2: assistant's genuine gold reversal turn!
                tpl = rng.choice(
                    DEV_REVERSALS_TEMPLATES if split == "dev" else TEST_REVERSALS_TEMPLATES
                )
                reversal_text = format_template(rng, split, tpl)
                reversal_turn_uuid = deterministic_uuid(f"session_{session_idx}_turn_{t_idx + 2}")
                planted_decisions.append((reversal_turn_uuid, 0, "reversal"))
                reversals_planted += 1

                retry_tu_id = f"tu_{session_idx}_{t_idx}_retry"
                if rng.random() < 0.25:
                    new_cmd = cmd
                else:
                    new_cmd = rng.choice(cmd_pool)
                    while new_cmd == cmd:
                        new_cmd = rng.choice(cmd_pool)
                tool_result_2 = {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": reversal_text,
                            },
                            {
                                "type": "tool_use",
                                "id": retry_tu_id,
                                "name": tool_name,
                                "input": {"command": new_cmd},
                            },
                        ],
                        "usage": generate_assistant_usage(rng),
                        "model": "claude-3-5-sonnet",
                    },
                }

                success_pool = DEV_SUCCESS_MESSAGES if split == "dev" else TEST_SUCCESS_MESSAGES
                success_content_reversal = rng.choice(success_pool)
                tool_result_3 = {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": retry_tu_id,
                                "content": success_content_reversal,
                            }
                        ],
                    },
                }
                pending_turns.extend([tool_result_1, tool_result_2, tool_result_3])

            # Normal gold reversal
            elif need_to_plant_reversals > 0 and (
                rng.random() < 0.20 or remaining_assistant_turns <= need_to_plant_reversals
            ):
                tpl = rng.choice(
                    DEV_REVERSALS_TEMPLATES if split == "dev" else TEST_REVERSALS_TEMPLATES
                )
                text = format_template(rng, split, tpl)
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }
                planted_decisions.append((turn_uuid, 0, "reversal"))
                reversals_planted += 1

            elif rng.random() < 0.25 and (num_turns - t_idx) >= 4:
                # Distractor: Flaky tool call retried unchanged
                tool_use_id = f"tu_{session_idx}_{t_idx}"
                cmd_pool = DEV_COMMANDS if split == "dev" else TEST_COMMANDS
                cmd = rng.choice(cmd_pool)
                opening_pool = DEV_OPENING_TEXTS if split == "dev" else TEST_OPENING_TEXTS
                text = rng.choice(opening_pool)
                tool_name = "bash" if split == "dev" else "sh"
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": text},
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": tool_name,
                            "input": {"command": cmd},
                        },
                    ],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }

                # Queue next 3 turns to complete the sequence
                error_pool = DEV_TOOL_ERRORS if split == "dev" else TEST_TOOL_ERRORS
                err_content = rng.choice(error_pool)
                tool_result_1 = {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": err_content,
                            }
                        ],
                    },
                }

                retry_tu_id = f"tu_{session_idx}_{t_idx}_retry"
                retry_pool = DEV_RETRY_PROSE if split == "dev" else TEST_RETRY_PROSE
                tpl = rng.choice(retry_pool)
                retry_text = format_template(rng, split, tpl)
                if rng.random() < 0.25:
                    new_cmd = rng.choice(cmd_pool)
                    while new_cmd == cmd:
                        new_cmd = rng.choice(cmd_pool)
                else:
                    new_cmd = cmd
                tool_result_2 = {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": retry_text,
                            },
                            {
                                "type": "tool_use",
                                "id": retry_tu_id,
                                "name": tool_name,
                                "input": {"command": new_cmd},
                            },
                        ],
                        "usage": generate_assistant_usage(rng),
                        "model": "claude-3-5-sonnet",
                    },
                }

                success_pool = DEV_SUCCESS_MESSAGES if split == "dev" else TEST_SUCCESS_MESSAGES
                success_content_retry = rng.choice(success_pool)
                tool_result_3 = {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": retry_tu_id,
                                "content": success_content_retry,
                            }
                        ],
                    },
                }
                pending_turns.extend([tool_result_1, tool_result_2, tool_result_3])

            elif rng.random() < 0.4:
                # Distractor: Assistant enumerating alternatives inside plan
                tpl = (
                    "To implement {task_desc}, we have a few options under "
                    "consideration: 1. {plan_a}, 2. {plan_b}, or 3. {plan_c}. "
                    "I will analyze these alternatives."
                )
                text = format_template(rng, split, tpl)
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }

            elif rng.random() < 0.55:
                # Distractor: Historical narration of past decisions
                tpls = [
                    (
                        "Historically, the system used {old_narrative} "
                        "because we didn't have {new_narrative} back then."
                    ),
                    ("We moved off {old_narrative} last year because of performance limits."),
                ]
                tpl = rng.choice(tpls)
                text = format_template(rng, split, tpl)
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }

            elif rng.random() < 0.7:
                # Distractor: Assistant typo correction
                tpl = (
                    "Ah, I made a small syntax error in {file_name} "
                    "where I wrote {bad_syntax}. I will correct it to "
                    "{good_syntax}."
                )
                text = format_template(rng, split, tpl)
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }

            elif rng.random() < 0.85:
                # Distractor: Simple assistant plan with no choices
                text = rng.choice(DEV_PLAN if split == "dev" else TEST_PLAN)
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }

            else:
                # Standard assistant response
                responses = DEV_RESPONSES if split == "dev" else TEST_RESPONSES
                text = rng.choice(responses)
                turn_obj["message"] = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": generate_assistant_usage(rng),
                    "model": "claude-3-5-sonnet",
                }

        line_objs.append(turn_obj)
        last_uuid = turn_uuid
        timestamp += rng.randint(2, 10)
        t_idx += 1

    bytes_data = bytearray()
    for obj in line_objs:
        line_str = json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"
        bytes_data.extend(line_str.encode("utf-8"))

    return Case(transcript_bytes=bytes(bytes_data), planted_decisions=planted_decisions)


def generate(seed: int, n: int, split: str | None = None) -> list[Case]:
    """Generate a split of synthetic cases deterministically based on seed."""
    if split is None:
        split = "test" if seed >= 20000 else "dev"

    cases = []
    for i in range(n):
        # Derive a stable seed per session to ensure order-independence and
        # across-process determinism.
        session_seed = int(hashlib.sha256(f"{seed}_{i}".encode()).hexdigest(), 16) % (2**31)
        rng = random.Random(session_seed)
        cases.append(generate_session(rng, i, split))
    return cases


def resolve_gold_for_case(case: Case) -> list[Decision]:
    """Resolve content-derived Block.block_ids for planted decisions using the ordinary adapter."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp.write(case.transcript_bytes)
        tmp_path = tmp.name

    try:
        adapter = adapters.get("claude-code")
        session = adapter.parse(tmp_path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    # Map turn UUID to parsed turn object
    turn_by_uuid = {t.uuid: t for t in session.turns if t.uuid}
    resolved = []

    for uuid, block_idx, kind in case.planted_decisions:
        if uuid not in turn_by_uuid:
            raise ValueError(f"Planted turn UUID {uuid!r} not found in parsed session turns.")
        turn = turn_by_uuid[uuid]
        if block_idx < 0 or block_idx >= len(turn.blocks):
            raise ValueError(
                f"Planted block index {block_idx} out of range "
                f"(0-{len(turn.blocks) - 1}) for turn UUID {uuid!r}."
            )
        block = turn.blocks[block_idx]
        resolved.append(Decision(kind=kind, source_ref=block.block_id))

    return resolved


def run_gate(
    extractor: Callable[[Session], list[Decision]],
    seed: int,
    split: str,
    n: int,
) -> dict[str, Any]:
    """Score an extractor over a whole split, overall and per slice."""
    all_gold: list[Decision] = []
    all_preds: list[Decision] = []
    block_is_post_failure: dict[str, bool] = {}

    for case in generate(seed=seed, n=n, split=split):
        all_gold.extend(resolve_gold_for_case(case))

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
            tmp.write(case.transcript_bytes)
            tmp_path = tmp.name
        try:
            session = adapters.get("claude-code").parse(tmp_path)
            block_is_post_failure.update(post_failure_blocks(session))
            all_preds.extend(extractor(session))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    return score_with_slices(all_preds, all_gold, block_is_post_failure)


def baseline_nothing(session: Session) -> list[Decision]:
    """Baseline: predicts no decisions at all. Always fails."""
    return []


def baseline_naive(session: Session) -> list[Decision]:
    """Baseline: flags the first assistant block after any failed tool result."""
    decisions_list = []

    for idx, turn in enumerate(session.turns):
        is_failed_tool = False
        if turn.role == "user":
            for block in turn.blocks:
                if block.kind == "tool_result" and (
                    "failed" in block.text or "Exit code 1" in block.text
                ):
                    is_failed_tool = True
                    break

        if is_failed_tool:
            # Find the first assistant turn in sequence after this turn
            for next_turn in session.turns[idx + 1 :]:
                if next_turn.role == "assistant":
                    if next_turn.blocks:
                        first_block = next_turn.blocks[0]
                        decisions_list.append(
                            Decision(kind="reversal", source_ref=first_block.block_id)
                        )
                    break

    return decisions_list


def baseline_leak(session: Session) -> list[Decision]:
    """Baseline: flags a reversal when a failed tool_result precedes

    and the assistant's turn does not repeat the command.
    """
    decisions_list = []

    for idx, turn in enumerate(session.turns):
        if turn.role != "assistant":
            continue

        # Rule 1: Preceding tool_result is a failure (at turn index idx - 1)
        if idx - 1 < 0:
            continue
        prev_turn = session.turns[idx - 1]
        if prev_turn.role != "user":
            continue

        is_failed_tool = False
        for block in prev_turn.blocks:
            if block.kind == "tool_result" and (
                "failed" in block.text or "Exit code 1" in block.text
            ):
                is_failed_tool = True
                break

        if not is_failed_tool:
            continue

        # Rule 2: Does not repeat the previous command (from turn index idx - 2)
        if idx - 2 < 0:
            continue
        prev_prev_turn = session.turns[idx - 2]
        if prev_prev_turn.role != "assistant":
            continue

        curr_cmd = None
        for block in turn.blocks:
            if block.kind == "tool_use":
                curr_cmd = block.text
                break

        prev_cmd = None
        for block in prev_prev_turn.blocks:
            if block.kind == "tool_use":
                prev_cmd = block.text
                break

        if curr_cmd is not None and prev_cmd is not None and curr_cmd != prev_cmd and turn.blocks:
            decisions_list.append(Decision(kind="reversal", source_ref=turn.blocks[0].block_id))

    return decisions_list
