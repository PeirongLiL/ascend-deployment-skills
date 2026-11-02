#!/usr/bin/env python3
"""Opt-in live HBM write/readback + known-result compute check; run on an idle NPU."""
import argparse
import json
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", type=int, required=True, help="logical device inside current runtime; record physical mapping separately")
    p.add_argument("--max-gib", type=float, required=True)
    p.add_argument("--reserve-gib", type=float, default=8)
    p.add_argument("--passes", type=int, default=3)
    p.add_argument("--max-seconds", type=float, default=180)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    if not a.execute:
        p.error("requires --execute after confirming idle device and authorization")
    if a.max_gib <= 0 or a.reserve_gib < 1 or a.passes < 1 or a.max_seconds <= 0 or a.device < 0:
        p.error("invalid limits")
    import torch
    import torch_npu  # noqa: F401
    gib = 1024 ** 3
    block_bytes = 64 * 1024 ** 2
    n = block_bytes // 4
    blocks_needed = int(a.max_gib * gib // block_bytes)
    if blocks_needed < 1:
        p.error("max-gib must allow at least one 64MiB block")
    torch.npu.set_device(a.device)
    free, total = torch.npu.mem_get_info()
    if blocks_needed * block_bytes + a.reserve_gib * gib > free:
        raise RuntimeError("insufficient free HBM for requested coverage plus reserve; do not evict other processes")
    start = time.monotonic()
    def budget():
        if time.monotonic() - start > a.max_seconds:
            raise TimeoutError("verification incomplete: runtime limit")
    def pattern(i):
        # Independent host-derived reference per chunk, not a shared device reference.
        return torch.bitwise_xor(torch.arange(n, dtype=torch.int32), (i * 104729 + 7919) & 0x7fffffff)
    blocks = []
    print(json.dumps({"logical_device": a.device, "total_gib": total / gib, "free_gib": free / gib,
                      "target_gib": blocks_needed * block_bytes / gib, "passes": a.passes}), flush=True)
    for i in range(blocks_needed):
        budget()
        free, _ = torch.npu.mem_get_info()
        if free - block_bytes < a.reserve_gib * gib:
            raise RuntimeError("verification incomplete: reserve reached before requested coverage")
        blocks.append(pattern(i).to("npu:" + str(a.device)))
        torch.npu.synchronize()
    for iteration in range(a.passes):
        for i, block in enumerate(blocks):
            budget()
            observed = block.cpu()
            if not torch.equal(observed, pattern(i)):
                print(json.dumps({"result": "DATA_CORRUPTION", "pass": iteration + 1, "block": i}), flush=True)
                return 3
        print(json.dumps({"pass": iteration + 1, "mismatches": 0}), flush=True)
    budget()
    x = torch.ones((256, 256), dtype=torch.float16, device="npu:" + str(a.device))
    y = (x @ x).cpu()
    if not torch.equal(y, torch.full((256, 256), 256, dtype=torch.float16)):
        raise RuntimeError("known-result matrix computation mismatch")
    print(json.dumps({"result": "PASS", "filled_gib": len(blocks) * block_bytes / gib,
                      "passes": a.passes, "mismatches": 0, "known_result_compute": "PASS",
                      "note": "collect post-test ECC separately; not full-server acceptance"}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"result": "FAILED_OR_INCOMPLETE", "error": type(exc).__name__ + ": " + str(exc)[:500]}), flush=True)
        sys.exit(2)
