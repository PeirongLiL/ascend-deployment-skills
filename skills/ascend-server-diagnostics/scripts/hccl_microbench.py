#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import time
from pathlib import Path


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def parse_size(text: str) -> int:
    suffix = text[-1].upper()
    if suffix in {"K", "M", "G"}:
        value = float(text[:-1])
        scale = {"K": 1024, "M": 1024**2, "G": 1024**3}[suffix]
        return int(value * scale)
    return int(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ops", default="all_to_all,all_reduce,all_gather,reduce_scatter")
    parser.add_argument("--sizes", default="4K,64K,1M,4M,16M,64M")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--iters", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="confirm idle devices and authorization")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    if not args.execute:
        parser.error("requires --execute after confirming idle devices and authorization")
    if args.timeout_seconds <= 0 or args.iters < 1 or args.warmup < 0:
        parser.error("invalid timeout/iterations/warmup")
    if args.output.exists():
        parser.error("output already exists; select a new run directory")
    import torch
    import torch.distributed as dist
    import torch_npu  # noqa: F401

    local_rank = int(os.environ["LOCAL_RANK"])
    rank = int(os.environ["RANK"])
    world = int(os.environ["WORLD_SIZE"])
    torch.npu.set_device(local_rank)
    dist.init_process_group("hccl", timeout=dt.timedelta(seconds=args.timeout_seconds))
    device = torch.device(f"npu:{local_rank}")
    dtype = torch.bfloat16
    dtype_bytes = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)

    for op in args.ops.split(","):
        for label in args.sizes.split(","):
            total_bytes = parse_size(label)
            if op == "all_gather":
                input_numel = max(1, total_bytes // dtype_bytes // world)
                output_numel = input_numel * world
            elif op == "reduce_scatter":
                input_numel = max(world, total_bytes // dtype_bytes)
                input_numel = (input_numel // world) * world
                output_numel = input_numel // world
            else:
                input_numel = max(world, total_bytes // dtype_bytes)
                input_numel = (input_numel // world) * world
                output_numel = input_numel
            input_bytes = input_numel * dtype_bytes
            output_bytes = output_numel * dtype_bytes
            if op == "all_reduce":
                estimated_wire_bytes = input_bytes * 2 * (world - 1) / world
            elif op == "all_to_all":
                estimated_wire_bytes = input_bytes * (world - 1) / world
            elif op == "all_gather":
                estimated_wire_bytes = input_bytes * (world - 1)
            elif op == "reduce_scatter":
                estimated_wire_bytes = input_bytes * (world - 1) / world
            else:
                raise ValueError(op)

            inp = torch.full((input_numel,), float(rank % 7), dtype=dtype, device=device)
            out = torch.empty((output_numel,), dtype=dtype, device=device)

            def run_once() -> None:
                if op == "all_to_all":
                    dist.all_to_all_single(out, inp)
                elif op == "all_reduce":
                    out.copy_(inp)
                    dist.all_reduce(out)
                elif op == "all_gather":
                    dist.all_gather_into_tensor(out, inp)
                elif op == "reduce_scatter":
                    dist.reduce_scatter_tensor(out, inp)
                else:
                    raise ValueError(op)

            dist.barrier()
            for _ in range(args.warmup):
                run_once()
            torch.npu.synchronize()

            # Fail the run if the collective completed but returned bad data.
            if op == "all_to_all":
                chunk = input_numel // world
                observed = out[::chunk].float()
                expected = torch.tensor(
                    [float(source % 7) for source in range(world)],
                    dtype=torch.float32,
                    device=device,
                )
                local_ok = bool(torch.allclose(observed, expected))
            elif op in {"all_reduce", "reduce_scatter"}:
                expected_value = float(sum(source % 7 for source in range(world)))
                local_ok = bool(torch.isclose(out[0].float(), torch.tensor(expected_value, device=device)))
            else:
                observed = out[::input_numel].float()
                expected = torch.tensor(
                    [float(source % 7) for source in range(world)],
                    dtype=torch.float32,
                    device=device,
                )
                local_ok = bool(torch.allclose(observed, expected))
            global_ok = torch.tensor([int(local_ok)], dtype=torch.int32, device=device)
            dist.all_reduce(global_ok, op=dist.ReduceOp.MIN)
            if not bool(global_ok.item()):
                raise RuntimeError(f"collective validation failed: op={op} size={label}")

            dist.barrier()
            samples = []
            for _ in range(args.iters):
                torch.npu.synchronize()
                started = time.perf_counter()
                run_once()
                torch.npu.synchronize()
                samples.append(time.perf_counter() - started)
            local = torch.tensor(
                [statistics.fmean(samples), percentile(samples, 0.5), percentile(samples, 0.99), max(samples)],
                dtype=torch.float32,
                device=device,
            )
            gathered = [torch.empty_like(local) for _ in range(world)]
            dist.all_gather(gathered, local)
            if rank == 0:
                rows = [tensor.cpu().tolist() for tensor in gathered]
                means = [row[0] for row in rows]
                med = statistics.median(means)
                result = {
                    "op": op,
                    "size_label": label,
                    "input_bytes_per_rank": input_bytes,
                    "output_bytes_per_rank": output_bytes,
                    "estimated_wire_bytes_per_rank": estimated_wire_bytes,
                    "world_size": world,
                    "warmup": args.warmup,
                    "iters": args.iters,
                    "validated": True,
                    "rank_mean_seconds_min": min(means),
                    "rank_mean_seconds_median": med,
                    "rank_mean_seconds_p99": percentile(means, 0.99),
                    "rank_mean_seconds_max": max(means),
                    "straggler_ratio": max(means) / med if med else 0,
                    "effective_wire_gbps_median": (estimated_wire_bytes * 8 / 1e9) / med if med else 0,
                    "per_rank": rows,
                }
                with args.output.open("a") as handle:
                    handle.write(json.dumps(result, sort_keys=True) + "\n")
                print(json.dumps(result, sort_keys=True), flush=True)
            del inp, out
            torch.npu.empty_cache()
            dist.barrier()

    dist.destroy_process_group()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
