#!/usr/bin/env python3
"""Live OpenAI-compatible streaming benchmark. Python standard library only."""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def valid_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("base_url must be HTTP(S), without embedded credentials, query or fragment")
    return url.rstrip("/").removesuffix("/v1")


def headers(c):
    result = {"Content-Type": "application/json"}
    key = os.environ.get(c.get("api_key_env", "LLM_BENCH_API_KEY"))
    if key:
        result["Authorization"] = "Bearer " + key
    return result


def request(c, path, payload=None, timeout=8):
    url = valid_url(c["base_url"]) + path
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers(c))
    with urllib.request.urlopen(req, timeout=max(0.05, timeout)) as r:
        raw = r.read(16 * 1024 * 1024).decode()
        return json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw


def cache_counters(text):
    import re
    counters = {"queries": 0.0, "hits": 0.0}
    found = set()
    for line in str(text).splitlines():
        for name, short in [("queries", "queries"), ("hits", "hits")]:
            if re.match(r"^vllm:prefix_cache_" + name + r"_total(?:\{|\s)", line):
                counters[short] += float(line.rsplit(None, 1)[-1])
                found.add(short)
    return counters if len(found) == 2 else None


def inspect(c, deadline=None):
    out = {"captured_at": now(), "base_url": valid_url(c["base_url"])}
    for key, path in [("health", "/health"), ("models", "/v1/models"), ("metrics", "/metrics")]:
        try:
            timeout = min(8, deadline - time.monotonic()) if deadline else 8
            if timeout <= 0:
                raise TimeoutError("run budget exhausted")
            value = request(c, path, timeout=timeout)
            out[key] = cache_counters(value) if key == "metrics" else value
        except Exception as exc:
            out[key] = None
            out[key + "_error"] = str(exc)[:300]
    return out


def validate(c):
    valid_url(c["base_url"])
    if not isinstance(c.get("model"), str) or not c["model"]:
        raise ValueError("model is required")
    if c.get("endpoint", "completions") not in {"completions", "chat"}:
        raise ValueError("endpoint must be completions or chat")
    if c.get("cache_mode", "isolated") not in {"isolated", "shared_prefix", "uncontrolled"}:
        raise ValueError("unsupported cache_mode")
    if not 0 <= c.get("shared_prefix_ratio", 0.99) <= 1:
        raise ValueError("shared_prefix_ratio must be in [0,1]")
    for k in ["request_timeout_seconds", "maximum_runtime_seconds", "repeats", "max_consecutive_failures"]:
        if float(c.get(k, {"request_timeout_seconds": 120, "maximum_runtime_seconds": 600, "repeats": 1, "max_consecutive_failures": 3}[k])) <= 0:
            raise ValueError(k + " must be positive")
    for k, default in [("repeats", 1), ("max_consecutive_failures", 3), ("warmup_requests", 4)]:
        v = c.get(k, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < (0 if k == "warmup_requests" else 1):
            raise ValueError(k + " must be an integer in range")
    if "warmup_output_tokens" in c and (not isinstance(c["warmup_output_tokens"], int) or c["warmup_output_tokens"] <= 0):
        raise ValueError("warmup_output_tokens must be a positive integer")
    if int(c.get("warmup_requests", 4)) < 0:
        raise ValueError("warmup_requests must be nonnegative")
    if not c.get("cases"):
        raise ValueError("cases must not be empty")
    for case in c["cases"]:
        for k in ["input_tokens", "output_tokens", "concurrency", "requests"]:
            if not isinstance(case.get(k), int) or isinstance(case[k], bool) or case[k] <= 0:
                raise ValueError("case " + k + " must be a positive integer")
        if c.get("service_max_model_len") and case["input_tokens"] + case["output_tokens"] > c["service_max_model_len"]:
            raise ValueError("input + output exceeds configured service context")
        if c.get("service_max_model_len") and case["input_tokens"] + c.get("warmup_output_tokens", case["output_tokens"]) > c["service_max_model_len"]:
            raise ValueError("warmup input + output exceeds configured service context")
    if c.get("endpoint") == "chat" and not c.get("prompt_text"):
        raise ValueError("chat requires prompt_text, including calibrated template overhead")
    forbidden = {"api_key", "token", "authorization", "password"}.intersection(c)
    if forbidden:
        raise ValueError("use api_key_env, never store credentials in config")
    for key, value in c.get("slo", {}).items():
        if key not in {"ttft_seconds", "ttfc_seconds", "tpot_seconds", "latency_seconds"} or value <= 0:
            raise ValueError("unsupported or nonpositive SLO")


def plan(c):
    validate(c)
    repeats = int(c.get("repeats", 1))
    return {"model": c["model"], "base_url": valid_url(c["base_url"]),
            "cases": len(c["cases"]) * repeats,
            "formal_requests": sum(x["requests"] for x in c["cases"]) * repeats,
            "warmup_requests": len(c["cases"]) * repeats * int(c.get("warmup_requests", 4)),
            "maximum_runtime_seconds": c.get("maximum_runtime_seconds", 600),
            "cache_mode": c.get("cache_mode", "isolated"),
            "isolated_cache_support_confirmed": c.get("cache_salt_supported") is True,
            "note": "plan only; no network requests or service changes"}


def pct(values, q):
    a = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not a:
        return None
    p = (len(a) - 1) * q
    lo, hi = math.floor(p), math.ceil(p)
    return a[lo] + (a[hi] - a[lo]) * (p - lo)


def mean(values):
    a = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return statistics.fmean(a) if a else None


def summarize(c, case, rows, elapsed):
    good = [r for r in rows if r.get("success") is True]
    slo = c.get("slo", {})
    qualifying = [r for r in good if all(r.get(k) is not None and r[k] <= v for k, v in slo.items())] if slo else None
    div = lambda n: n / elapsed if elapsed and elapsed > 0 else None
    value = lambda k: [r.get(k) for r in good]
    output = sum(r["completion_tokens"] for r in good)
    input_n = sum(r["prompt_tokens"] for r in good)
    row = {"模型": c["model"], "并行度": case["concurrency"], "提示长度": case["input_tokens"],
           "输出长度": case["output_tokens"], "测试总时长(s)": elapsed,
           "总请求数": len(rows), "成功请求数": len(good), "失败请求数": len(rows) - len(good),
           "平均吞吐（token/s）": mean([r["completion_tokens"] / r["latency_seconds"] for r in good if r["latency_seconds"] > 0]),
           "总吞吐量(token/s)": div(output), "平均QPS": div(len(good)),
           "Goodput(QPS)": div(len(qualifying)) if qualifying is not None else None,
           "平均延迟(s)": mean(value("latency_seconds")), "首token平均延迟(s)": mean(value("ttft_seconds")),
           "单token平均延迟(s)": mean(value("tpot_seconds")), "平均输入token数": mean(value("prompt_tokens")),
           "平均输出token数": mean(value("completion_tokens")), "API URL": valid_url(c["base_url"]) + ("/v1/chat/completions" if c.get("endpoint") == "chat" else "/v1/completions"),
           "温度": c.get("temperature", 0), "流式输出": True}
    for label, q in [("P50", .5), ("P90", .9), ("P99", .99)]:
        for name, key in [("延迟(s)", "latency_seconds"), ("首token延迟(s)", "ttft_seconds"), ("单token延迟(s)", "tpot_seconds")]:
            row[label + name] = pct(value(key), q)
    return {"row": row, "total_token_throughput": div(output + input_n),
            "mean_decode_tokens_per_second": mean(value("decode_tokens_per_second")),
            "good_requests": len(qualifying) if qualifying is not None else None,
            "good_ratio_all_requests": len(qualifying) / len(rows) if qualifying is not None and rows else None,
            "good_ratio_successful_requests": len(qualifying) / len(good) if qualifying is not None and good else None,
            "sample_count": len(good), "planned_requests": case["requests"],
            "complete": len(rows) == case["requests"], "all_successful": len(rows) == case["requests"] == len(good),
            "slo": slo, "small_sample_warning": len(good) < 100,
            "timing_basis": "last_nonempty_output_for_decode;stream_end_for_e2e"}


def token_seed(c, case, nonce, deadline):
    if c.get("endpoint") == "chat":
        return None
    text = "Benchmark " + nonce + ". Explain reliable distributed computing step by step. "
    result = request(c, "/tokenize", {"model": c["model"], "prompt": text}, timeout=min(30, max(.05, deadline - time.monotonic())))
    ids = result.get("tokens") if isinstance(result, dict) else None
    if not ids or not all(isinstance(x, int) for x in ids):
        raise ValueError("/tokenize must return token IDs; refusing approximate input length")
    return (ids * math.ceil(case["input_tokens"] / len(ids)))[:case["input_tokens"]]


def one_request(c, case, tokens, rid, nonce, deadline, output_tokens):
    start = time.monotonic()
    record = {"request_id": rid, "started_at_epoch": time.time(), "success": False,
              "prompt_tokens": None, "completion_tokens": None, "ttft_seconds": None,
              "ttfc_seconds": None, "tpot_seconds": None, "decode_tokens_per_second": None,
              "latency_seconds": None, "error": None}
    conn = None
    try:
        timeout = min(float(c.get("request_timeout_seconds", 120)), deadline - start)
        if timeout <= 0:
            raise TimeoutError("run budget exhausted")
        stop = min(deadline, start + timeout)
        body = {"model": c["model"], "max_tokens": output_tokens, "temperature": c.get("temperature", 0),
                "stream": True, "stream_options": {"include_usage": True}, "seed": int(c.get("seed", 42)) + abs(rid)}
        strict = c.get("strict_output", True)
        if strict:
            body["ignore_eos"] = True
        if c.get("endpoint") == "chat":
            body["messages"] = [{"role": "user", "content": c["prompt_text"]}]
        else:
            prompt = tokens.copy()
            if c.get("cache_mode", "isolated") == "shared_prefix":
                common = round(len(prompt) * c.get("shared_prefix_ratio", .99))
                # Vary the suffix using valid IDs from this model's own tokenization.
                suffix = prompt[common:]
                if len(suffix) > 1:
                    shift = (abs(rid) * 17 + 1) % len(suffix)
                    prompt[common:] = suffix[shift:] + suffix[:shift]
            body["prompt"] = prompt
        if c.get("cache_mode", "isolated") == "isolated":
            body["cache_salt"] = hashlib.sha256(f"{nonce}:{rid}".encode()).hexdigest()
        record["prompt_sha256"] = hashlib.sha256(json.dumps(body.get("prompt", body.get("messages"))).encode()).hexdigest()
        p = urllib.parse.urlsplit(valid_url(c["base_url"]))
        cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        conn = cls(p.hostname, p.port, timeout=timeout)
        path = p.path + ("/v1/chat/completions" if c.get("endpoint") == "chat" else "/v1/completions")
        conn.request("POST", path, json.dumps(body), headers(c))
        sock = conn.sock
        response = conn.getresponse()
        record["http_status"] = response.status
        if response.status != 200:
            raise RuntimeError("HTTP " + str(response.status))
        first = first_content = last = None
        done, finish, usage = False, None, {}
        while True:
            remaining = stop - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request or run budget exhausted")
            if sock:
                sock.settimeout(remaining)
            line = response.readline(1024 * 1024)
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                done = True
                break
            obj = json.loads(payload)
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or {}
                content = choice.get("text") if c.get("endpoint") != "chat" else delta.get("content")
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                t = time.monotonic()
                if (isinstance(content, str) and content) or (isinstance(reasoning, str) and reasoning):
                    first = t if first is None else first
                    last = t
                if isinstance(content, str) and content:
                    first_content = t if first_content is None else first_content
                finish = choice.get("finish_reason") or finish
        n_in, n_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        record.update(prompt_tokens=n_in, completion_tokens=n_out, stream_complete=done, finish_reason=finish)
        expected = case.get("expected_prompt_tokens", c.get("expected_prompt_tokens", case["input_tokens"]))
        if not (isinstance(n_in, int) and n_in > 0 and isinstance(n_out, int) and n_out > 0 and first is not None and done):
            raise ValueError("incomplete stream or unavailable usage")
        if n_in != expected:
            raise ValueError(f"input length mismatch: actual={n_in}, expected={expected}")
        if strict and (n_out != output_tokens or finish != "length"):
            raise ValueError(f"output length mismatch: actual={n_out}, expected={output_tokens}, finish={finish}")
        if not strict and finish not in {"stop", "length"}:
            raise ValueError("unexpected finish_reason: " + str(finish))
        record.update(success=True, ttft_seconds=first - start,
                      ttfc_seconds=first_content - start if first_content else None)
        if n_out > 1 and last is not None and last > first:
            record["tpot_seconds"] = (last - first) / (n_out - 1)
            record["decode_tokens_per_second"] = (n_out - 1) / (last - first)
    except Exception as exc:
        record["error"] = type(exc).__name__ + ": " + str(exc)[:300]
    finally:
        record["latency_seconds"] = time.monotonic() - start
        if conn:
            conn.close()
    return record


def run_phase(c, case, tokens, nonce, deadline, count, phase, tag, raw):
    rows, pending, next_id, failed = [], {}, 0, 0
    width = min(case["concurrency"], count)
    start = time.monotonic()
    if width <= 0:
        return rows, 0.0
    with futures.ThreadPoolExecutor(max_workers=width) as pool:
        while pending or (next_id < count and time.monotonic() < deadline and failed < c.get("max_consecutive_failures", 3)):
            while next_id < count and len(pending) < width and time.monotonic() < deadline and failed < c.get("max_consecutive_failures", 3):
                rid = next_id + (0 if phase == "formal" else 100000000)
                n_out = case["output_tokens"] if phase == "formal" else c.get("warmup_output_tokens", case["output_tokens"])
                f = pool.submit(one_request, c, case, tokens, rid, nonce + tag, deadline, n_out)
                pending[f] = rid
                next_id += 1
            if not pending:
                break
            finished, _ = futures.wait(pending, return_when=futures.FIRST_COMPLETED)
            for f in finished:
                pending.pop(f)
                row = f.result()
                row.update(phase=phase, case_id=tag)
                raw.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                raw.flush()
                rows.append(row)
                failed = 0 if row["success"] else failed + 1
    return rows, time.monotonic() - start


def write_report(out, summary):
    lines = ["# 在线模型性能测评", "", "状态：" + summary["state"], "",
             "| 工况 | 成功/已发出/计划 | 输出 token/s | TTFT 均值(s) | TPOT 均值(s) |", "| --- | --- | --- | --- | --- |"]
    for case in summary["cases"]:
        r = case["row"]
        fmt = lambda v: "未测得" if v is None else f"{v:.4f}"
        lines.append(f"| {case['case_id']} | {r['成功请求数']}/{r['总请求数']}/{case['planned_requests']} | {fmt(r['总吞吐量(token/s)'])} | {fmt(r['首token平均延迟(s)'])} | {fmt(r['单token平均延迟(s)'])} |")
    lines += ["", "- 缓存场景：" + summary["config"].get("cache_mode", "isolated"),
              "- SLO：" + json.dumps(summary["config"].get("slo", {}), ensure_ascii=False),
              "- 原始请求含预热，汇总仅统计 phase=formal；缺失值未补零。",
              "- 单次或小样本结果不代表稳定尾延迟；全局缓存计数可能受到其他业务影响。"]
    if summary.get("error"):
        lines += ["", "停止原因：" + summary["error"]]
    (out / "report.md").write_text("\n".join(lines) + "\n")


def live_run(c, out):
    validate(c)
    if c.get("cache_mode", "isolated") == "isolated" and c.get("cache_salt_supported") is not True:
        raise ValueError("confirm endpoint supports cache_salt and set cache_salt_supported=true; otherwise use uncontrolled and label cache conditions")
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + c.get("maximum_runtime_seconds", 600)
    nonce = uuid.uuid4().hex
    summary = {"state": "running", "started_at": now(), "config": c, "cases": [], "rows": []}
    manifest = {"started_at": now(), "run_id": nonce, "config": c,
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save(out / "manifest.json", manifest)
    try:
        current = inspect(c, deadline)
        save(out / "service-before.json", current)
        manifest["service_before"] = current
        ids = {x.get("id") for x in (current.get("models") or {}).get("data", [])}
        if c["model"] not in ids:
            raise RuntimeError("requested model not present in current /v1/models")
        served = next(x for x in current["models"]["data"] if x.get("id") == c["model"])
        limit = served.get("max_model_len")
        if isinstance(limit, int):
            for case in c["cases"]:
                if case["input_tokens"] + max(case["output_tokens"], c.get("warmup_output_tokens", case["output_tokens"])) > limit:
                    raise ValueError("input + output exceeds current server /v1/models context limit")
        with (out / "requests.jsonl").open("x") as raw:
            for repeat in range(int(c.get("repeats", 1))):
                for index, case in enumerate(c["cases"]):
                    tag = f"r{repeat + 1}-c{index + 1}-p{case['concurrency']}-i{case['input_tokens']}-o{case['output_tokens']}"
                    if time.monotonic() >= deadline:
                        raise TimeoutError("run budget exhausted before " + tag)
                    tokens = token_seed(c, case, nonce + tag, deadline)
                    warm, _ = run_phase(c, case, tokens, nonce, deadline, int(c.get("warmup_requests", 4)), "warmup", tag, raw)
                    if len(warm) != int(c.get("warmup_requests", 4)) or any(not x["success"] for x in warm):
                        raise RuntimeError("warmup failed or incomplete at " + tag)
                    before = None
                    try:
                        before = cache_counters(request(c, "/metrics", timeout=min(5, max(.05, deadline - time.monotonic()))))
                    except Exception:
                        pass
                    rows, elapsed = run_phase(c, case, tokens, nonce, deadline, case["requests"], "formal", tag, raw)
                    result = summarize(c, case, rows, elapsed)
                    result.update(case_id=tag, configuration=case, cache_before=before)
                    try:
                        after = cache_counters(request(c, "/metrics", timeout=min(5, max(.05, deadline - time.monotonic()))))
                        if before and after and all(after[k] >= before[k] for k in before):
                            delta = {k: after[k] - before[k] for k in before}
                            result["cache_counter_delta"] = delta
                            result["global_cache_hit_ratio"] = delta["hits"] / delta["queries"] if delta["queries"] > 0 else None
                    except Exception:
                        pass
                    summary["cases"].append(result)
                    summary["rows"].append(result["row"])
                    save(out / "summary.json", summary)
                    print(json.dumps({"case": tag, "complete": result["complete"], "successful": result["row"]["成功请求数"], "output_tps": result["row"]["总吞吐量(token/s)"]}), flush=True)
                    if not result["all_successful"]:
                        raise RuntimeError("case failed or incomplete: " + tag)
        summary["state"] = "completed"
    except (Exception, KeyboardInterrupt) as exc:
        summary["state"] = "incomplete"
        summary["error"] = type(exc).__name__ + ": " + str(exc)[:500]
    finally:
        summary["ended_at"] = now()
        summary["wall_seconds"] = time.monotonic() - started
        save(out / "manifest.json", manifest)
        save(out / "summary.json", summary)
        write_report(out, summary)
    return 0 if summary["state"] == "completed" else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    a = commands.add_parser("inspect", help="GET current health, models and cache metrics; no generation")
    a.add_argument("--base-url", required=True)
    a.add_argument("--api-key-env", default="LLM_BENCH_API_KEY")
    for name in ["plan", "run"]:
        a = commands.add_parser(name)
        a.add_argument("--config", type=Path, required=True)
        if name == "run":
            a.add_argument("--output-dir", type=Path, required=True)
            a.add_argument("--execute", action="store_true")
    a = commands.add_parser("summarize", help="recompute this runner's formal records using its manifest and original case wall times")
    a.add_argument("--run-dir", type=Path, required=True)
    a.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    if args.command == "inspect":
        result = inspect(vars(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("models") is not None else 2
    if args.command == "summarize":
        old = json.loads((args.run_dir / "summary.json").read_text())
        records = [json.loads(line) for line in (args.run_dir / "requests.jsonl").read_text().splitlines() if line]
        args.output_dir.mkdir(parents=True, exist_ok=False)
        new = {**old, "cases": [], "rows": [], "recomputed_from": str(args.run_dir.resolve())}
        for case in old["cases"]:
            rows = [r for r in records if r.get("phase") == "formal" and r.get("case_id") == case["case_id"]]
            item = summarize(old["config"], case["configuration"], rows, case["row"]["测试总时长(s)"])
            item.update(case_id=case["case_id"], configuration=case["configuration"])
            new["cases"].append(item)
            new["rows"].append(item["row"])
        save(args.output_dir / "summary.json", new)
        write_report(args.output_dir, new)
        return 0
    if args.command == "run" and not args.execute:
        p.error("run requires --execute after confirming workload and authorization")
    c = json.loads(args.config.read_text())
    if args.command == "plan":
        print(json.dumps(plan(c), ensure_ascii=False, indent=2))
        return 0
    return live_run(c, args.output_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(2)
