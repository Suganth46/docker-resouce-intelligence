"""Benchmark structured root-cause answers on the local Compose Ollama service."""

import argparse
import asyncio
import json
import math
from pathlib import Path
import re
from statistics import median
import time
from types import SimpleNamespace

from pydantic import ValidationError

from dri.core.llm_client import LLMError, OllamaClient
from dri.core.root_cause import ModelAnswer, build_prompt, grounded_summary, FAILURE_CLASSES

CANDIDATES = ["qwen3:0.6b", "gemma3:1b", "qwen2.5:1.5b", "qwen3:1.7b", "llama3.2:3b"]
KEYWORDS = dict(
    FAILURE_CLASSES, none=r"insufficient|healthy|no (?:evidence|failure|anomaly)|inconclusive"
)


async def docker(*args):
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await proc.communicate()
    return proc.returncode, output.decode(errors="replace")


async def watch_memory(samples, finished):
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    while not finished.is_set():
        code, out = await docker("stats", "--no-stream", "--format", "{{.MemUsage}}", "dri-ollama")
        match = re.match(r"([\d.]+)([KMG]?i?B)", out)
        if not code and match and match[2] in units:
            samples.append(float(match[1]) * units[match[2]] / 1024**2)
        try:
            await asyncio.wait_for(finished.wait(), 1)
        except asyncio.TimeoutError:
            pass


async def measure(model, url, cases):
    client = OllamaClient(url, model, 60, {}, probe_interval_seconds=0, failure_threshold=31)
    latencies, memory = [], []
    valid = grounded = correct = 0
    finished = asyncio.Event()
    sampler = None
    try:
        state = await client.probe()
        if state == "model_missing":
            code, _ = await docker("exec", "dri-ollama", "ollama", "pull", model)
            state = "pull failed" if code else await client.probe()
        if state != "ready":
            print(f"| {model} | 0 | — | — | — | — | — | — | {state} |", flush=True)
            return
        sampler = asyncio.create_task(watch_memory(memory, finished))
        for case in cases:
            report = SimpleNamespace(**case)
            system, user = build_prompt(report)
            for _ in range(3):
                start = time.monotonic()
                try:
                    data = await client.generate_json(system, user, ModelAnswer.model_json_schema())
                    answer = ModelAnswer.model_validate(data)
                except (LLMError, ValidationError):
                    latencies.append(time.monotonic() - start)
                    continue
                elapsed = time.monotonic() - start
                latencies.append(elapsed)
                valid += 1
                guarded = grounded_summary(answer, report, user, model, round(elapsed * 1000))
                contradicted = any(
                    key != report.verdict and re.search(pattern, answer.root_cause, re.I)
                    for key, pattern in FAILURE_CLASSES.items()
                )
                grounded += not contradicted and guarded.evidence_used == answer.evidence_used
                correct += bool(re.search(KEYWORDS[report.verdict], answer.root_cause, re.I))
        n = len(cases) * 3

        def rate(x):
            return f"{100*x/n:.1f}%"

        p50 = f"{median(latencies):.2f}" if latencies else "—"
        p95 = f"{sorted(latencies)[math.ceil(.95 * len(latencies)) - 1]:.2f}" if latencies else "—"
        peak = f"{max(memory):.0f}" if memory else "—"
        print(
            f"| {model} | {n} | {rate(valid)} | {rate(grounded)} | {rate(correct)} | {p50} | {p95} | {peak} | complete |",
            flush=True,
        )
    finally:
        finished.set()
        if sampler:
            await sampler
        await client.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--models", nargs="+", default=CANDIDATES)
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/llm_cases.json"
    cases = json.loads(path.read_text())
    print(
        "| Model | Trials | Valid JSON | Grounded | Correct class | p50 s | p95 s | Peak MiB | State |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for model in args.models:
        await measure(model, args.base_url, cases)


if __name__ == "__main__":
    asyncio.run(main())
