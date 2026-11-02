#!/usr/bin/env python3
"""Live, read-only Ascend SSH collector and evidence-based initial diagnosis."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def devices_arg(text):
    if text is None:
        return None
    if not re.fullmatch(r"\d+(,\d+)*", text):
        raise ValueError("devices must be comma-separated nonnegative integers")
    ids = list(dict.fromkeys(int(x) for x in text.split(",")))
    if len(ids) > 64 or any(i > 255 for i in ids):
        raise ValueError("device range too large")
    return ids


def safe_host(value):
    if not re.fullmatch(r"[A-Za-z0-9_@.\-:\[\]]+", value) or value.startswith("-"):
        raise ValueError("use a valid SSH host alias/address, without options or shell syntax")
    return value


def collector_script(devices=None, timeout=12):
    # Only static read-only commands are sent. No eval of remote output or user arguments.
    script = r'''#!/usr/bin/env bash
export LC_ALL=C
export PATH="$PATH:/usr/local/Ascend/driver/tools:/usr/local/Ascend/ascend-toolkit/latest/tools"
run() {
  local label="$1"; shift
  printf '\n@@BEGIN %s\n' "$label"
  timeout __TIMEOUT__ "$@" 2>&1
  local rc=$?
  printf '\n@@EXIT %s\n@@END\n' "$rc"
}
run identity bash -c 'hostname; date --iso-8601=seconds; uname -a; id -u'
run os cat /etc/os-release
run cpu-memory bash -c 'lscpu; free -b; df -h / /data /dev/shm'
run npu-summary npu-smi info
run npu-list npu-smi info -l
run versions bash -c 'for f in /usr/local/Ascend/driver/version.info /usr/local/Ascend/ascend-toolkit/latest/version.cfg /etc/ascend_install.info; do if test -r "$f"; then printf "\n%s\n" "$f"; head -60 "$f"; fi; done'
run docker docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
run interfaces ip -s -d link show
run addresses ip -br addr show
run listening-ports ss -lntp
run rdma rdma link show
run pcie lspci -nn
run storage lsblk -b -J -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,MODEL,SERIAL
run mounts findmnt -J
run md bash -c 'cat /proc/mdstat; if test -r /etc/mdadm.conf; then head -80 /etc/mdadm.conf; fi'
run lvm lvs -a -o lv_name,vg_name,segtype,stripes,stripesize,devices
run nvme-list bash -c 'if command -v nvme >/dev/null; then nvme list -o json; elif command -v smartctl >/dev/null; then smartctl --scan-open; else echo "nvme and smartctl unavailable"; exit 127; fi'
run kernel journalctl -k --since '-2 hours' -n 800 --no-pager
listing=$(timeout __TIMEOUT__ npu-smi info -l 2>/dev/null)
ids=$(printf '%s\n' "$listing" | sed -nE 's/^[[:space:]]*NPU ID[[:space:]]*:[[:space:]]*([0-9]+).*$/\1/p')
if test -z "$ids"; then
  ids=$(for f in /dev/davinci[0-9]*; do if test -e "$f"; then printf '%s\n' "${f#/dev/davinci}"; fi; done)
fi
__IDS_OVERRIDE__
query_help=$(timeout __TIMEOUT__ npu-smi info -h 2>/dev/null)
if [[ "$query_help" != *info-vdie* ]]; then
  run vdie-support bash -c 'echo "VDie query unsupported by this driver; use board Serial Number and PCIe slot for identity"; exit 3'
fi
if test -z "$ids"; then
  run no-devices bash -c 'echo "No NPU IDs discovered; provide --devices after checking actual topology"; exit 3'
fi
for i in $ids; do
  if [[ ! "$i" =~ ^[0-9]+$ ]]; then continue; fi
  run "health@$i" npu-smi info -t health -i "$i"
  run "ecc@$i" npu-smi info -t ecc -i "$i"
  run "board@$i" npu-smi info -t board -i "$i"
  if [[ "$query_help" == *info-vdie* ]]; then run "vdie@$i" npu-smi info -t info-vdie -i "$i"; fi
  run "processes@$i" npu-smi info -t proc-mem -i "$i"
  run "roce-link@$i" hccn_tool -i "$i" -link -g
  run "roce-health@$i" hccn_tool -i "$i" -net_health -g
  run "roce-ip@$i" hccn_tool -i "$i" -ip -g
  run "roce-optical@$i" hccn_tool -i "$i" -optical -g
  run "roce-stat@$i" hccn_tool -i "$i" -stat -g
  run "roce-link-stat@$i" hccn_tool -i "$i" -link_stat -g
done
for f in /proc/net/bonding/*; do if test -r "$f"; then run "bond@${f##*/}" cat "$f"; fi; done
for f in /sys/class/net/*; do
  if test -e "$f/device"; then run "ethtool@${f##*/}" ethtool -S "${f##*/}"; fi
done
for f in /sys/class/nvme/nvme[0-9]*; do
  if test -e "$f"; then
    if command -v nvme >/dev/null; then run "smart@${f##*/}" nvme smart-log "/dev/${f##*/}";
    else run "smart@${f##*/}" smartctl -a "/dev/${f##*/}"; fi
  fi
done
'''
    override = "ids=" + shlex.quote(" ".join(map(str, devices))) if devices is not None else "# use detected IDs"
    return script.replace("__TIMEOUT__", str(int(timeout))).replace("__IDS_OVERRIDE__", override)


def parse_sections(raw):
    result = {}
    for m in re.finditer(r"(?m)^@@BEGIN ([^\n]+)\n(.*?)\n@@EXIT (\d+)\n@@END", raw, re.S):
        result[m[1]] = {"text": m[2].strip(), "returncode": int(m[3]),
                        "raw_start_line": raw[:m.start()].count("\n") + 1}
    return result


def numbers(text):
    result = {}
    for line in text.splitlines():
        m = re.match(r"\s*(HBM [^:]+?)\s*:\s*(\d+)\s*$", line)
        if m:
            name = " ".join(m[1].split())
            result[name] = result.get(name, 0) + int(m[2])
    return result


def analyze(snapshot, expected=None):
    sections = snapshot["sections"]
    findings, cards = [], {}
    def add(category, state, message, key, severity="warning"):
        sec = sections.get(key, {})
        findings.append({"category": category, "state": state, "severity": severity,
                         "message": message, "evidence": key,
                         "raw_start_line": sec.get("raw_start_line")})
    if snapshot.get("transport_returncode") != 0:
        add("access", "未测试", "SSH/采集未完整成功，不能据此判断服务器正常", "transport", "error")
    if not sections:
        add("coverage", "未测试", "未获得任何结构化采集结果", "transport", "error")
    for key, sec in sections.items():
        if sec["returncode"] != 0:
            add("coverage", "未测试", f"{key} 查询失败，退出码 {sec['returncode']}；检查工具、权限及版本支持", key, "info")
            # smartctl uses a status bitmask: a nonzero exit can carry real disk faults.
            if not key.startswith("smart@"):
                continue
        text = sec["text"]
        if key.startswith("health@"):
            i = key.split("@")[1]
            states = re.findall(r"Health\s*(?:Status)?\s*:\s*([^\n]+)", text, re.I)
            health = "/".join(dict.fromkeys(s.strip() for s in states)) if states else None
            cards.setdefault(i, {})["health"] = health
            if health and health.upper() not in {"OK", "HEALTHY"}:
                add("npu", "确认异常", f"物理卡 {i} 健康状态为 {health}", key, "error")
            elif not health:
                add("coverage", "未测试", f"物理卡 {i} 健康字段无法解析", key, "info")
        elif key.startswith("ecc@"):
            i = key.split("@")[1]
            counts = numbers(text)
            cards.setdefault(i, {})["ecc"] = counts
            if not counts:
                add("coverage", "未测试", f"物理卡 {i} 未取得可解析 HBM ECC 字段", key, "info")
            for name, count in counts.items():
                if count:
                    aggregate = "Aggregate" in name
                    add("hbm", "疑似异常" if aggregate else "确认异常",
                        f"物理卡 {i}：{name}={count}" + ("（累计值，需结合身份与历史判断，非本轮新增证明）" if aggregate else ""),
                        key, "error" if "Double" in name and not aggregate else "warning")
        elif key.startswith("roce-link@"):
            i = key.split("@")[1]
            m = re.search(r"link status\s*:\s*(UP|DOWN)", text, re.I)
            link = m[1].upper() if m else None
            cards.setdefault(i, {})["roce_link"] = link
            if link == "DOWN":
                used = expected is not None and int(i) in expected
                add("roce", "确认异常" if used else "疑似异常",
                    f"物理卡 {i} RoCE 端口 DOWN" + ("，为预期业务端口" if used else "；需核实该端口是否参与当前业务"), key, "error" if used else "warning")
            elif link is None:
                add("coverage", "未测试", f"物理卡 {i} RoCE 链路字段无法解析", key, "info")
        elif key.startswith("roce-health@"):
            i = key.split("@")[1]
            m = re.search(r"net health status\s*:\s*([^\n]+)", text, re.I)
            value = m[1].strip() if m else None
            cards.setdefault(i, {})["roce_health"] = value
            if value and value.lower() == "init":
                add("coverage", "未测试", f"物理卡 {i} 网络健康为 Init：尚未取得已完成的网络健康检查状态，不能据此判物理链路损坏", key, "info")
            elif value and value.lower() not in {"success", "ok", "healthy"}:
                add("roce", "疑似异常", f"物理卡 {i} 网络健康：{value}，结合链路和参与端口进一步定位", key)
            elif value is None:
                add("coverage", "未测试", f"物理卡 {i} 网络健康字段无法解析", key, "info")
        elif key.startswith("roce-optical@"):
            for m in re.finditer(r"((?:Rx|Tx) (?:Los|LoL) Flag)\s*:\s*(0x[0-9a-f]+)", text, re.I):
                if int(m[2], 16):
                    add("optical", "疑似异常", f"{key}: {m[1]}={m[2]}，检查光模块/光纤/对端；不能仅据此指定损坏部件", key)
        elif key.startswith("vdie@") or key.startswith("board@"):
            i = key.split("@")[1]
            for m in re.finditer(r"(?im)^\s*(VDie ID|Serial Number|Serial No\.?|PCIe Bus Info|PCIe Bus ID)\s*:\s*([^\n]+)", text):
                cards.setdefault(i, {}).setdefault("identity", {})[m[1]] = " ".join(m[2].split())
            compatibility = re.search(r"(?im)^\s*Compatibility\s*:\s*([^\n]+)", text)
            if compatibility and compatibility[1].strip().upper() != "OK":
                add("runtime", "确认异常", f"物理卡 {i} 固件/软件兼容状态：{compatibility[1].strip()}", key, "error")
        elif key.startswith("processes@"):
            i = key.split("@")[1]
            processes = re.findall(r"Process id\s*:\s*(\d+)\s+Process name\s*:\s*(\S+)\s+Process memory\(MB\)\s*:\s*(\d+)", text)
            if processes:
                cards.setdefault(i, {})["processes"] = [{"pid": int(pid), "name": name, "memory_mb": int(mem)} for pid, name, mem in processes]
            elif re.search(r"no (?:running )?process|there is no.*process", text, re.I):
                cards.setdefault(i, {})["processes"] = []
            else:
                add("coverage", "未测试", f"物理卡 {i} 进程输出无法解析，不能判断设备空闲", key, "info")
        elif key == "kernel":
            relevant = [(n, line) for n, line in enumerate(text.splitlines(), 1)
                        if re.search(r"uncorrectable.*ecc|HBM.*ECC|PCIe.*fatal|nvme.*(?:timeout|reset)|I/O error|network port is down|EI0009", line, re.I)]
            if relevant:
                add("kernel", "疑似异常", f"当前采集的近两小时内核日志含 {len(relevant)} 条错误线索，需按时间和设备核对：" + relevant[0][1][:200], key)
        elif key.startswith("smart@"):
            if re.search(r"SMART overall-health.*FAILED|SMART Health Status\s*:\s*(?!OK\b)\S+", text, re.I):
                add("storage", "确认异常", f"{key}: SMART 明确报告健康失败", key, "error")
            for m in re.finditer(r"(?im)^\s*(critical_warning|critical warning|media_errors|media and data integrity errors)\s*:\s*(0x[0-9a-f]+|[0-9,]+)", text):
                count = int(m[2], 16) if m[2].lower().startswith("0x") else int(m[2].replace(",", ""))
                if count:
                    add("storage", "疑似异常", f"{key}: {m[1]}={m[2]}，累计项需复查增量", key)
    if not cards:
        add("coverage", "未测试", "未获得逐卡检查数据，不能给出 NPU 或 RoCE 验收结论", "npu-list", "error")
    if expected is not None:
        for i in expected:
            if str(i) not in cards or cards[str(i)].get("roce_link") is None:
                add("coverage", "未测试", f"预期业务端口 {i} 缺少可用链路检查结果", f"roce-link@{i}", "error")
    for i, card in cards.items():
        for field in ["health", "ecc", "roce_link", "roce_health"]:
            if field not in card:
                add("coverage", "未测试", f"物理卡 {i} 缺少 {field} 数据", f"{field}@{i}", "info")
    return {"captured_at": snapshot["captured_at"], "host": snapshot["host"], "cards": cards,
            "findings": findings, "scope": "current read-only snapshot; not stress or business acceptance",
            "stress_tests": {"hbm_readback": "未测试", "hccl_collective": "未测试", "disk_fio": "未测试"}}


def report(out, result):
    lines = ["# 昇腾服务器实时巡检", "", f"主机：{result['host']}  ", f"采集时间：{result['captured_at']}", "",
             "| 物理卡 | NPU 健康 | RoCE 链路 | 网络健康 | 运行进程数 |", "| --- | --- | --- | --- | --- |"]
    for i, c in sorted(result["cards"].items(), key=lambda x: int(x[0])):
        processes = len(c['processes']) if 'processes' in c else '未测得'
        lines.append(f"| {i} | {c.get('health') or '未测得'} | {c.get('roce_link') or '未测得'} | {c.get('roce_health') or '未测得'} | {processes} |")
    lines += ["", "## 异常与缺失项", ""]
    if not result["findings"]:
        lines.append("当前只读样本未见已识别异常；这不是压力测试或业务验收通过。")
    for f in result["findings"]:
        lines.append(f"- **{f['state']}**：{f['message']}。证据 `{f['evidence']}`，raw.log 起始行 {f.get('raw_start_line') or '无'}。")
    lines += ["", "## 后续检查", "",
              "- 显存：结合物理卡映射、当前/累计 ECC 与授权后的已知数据回读验证。",
              "- 通信：先确认业务使用端口，核对光模块与对端；链路正常但通信慢时做健康对照配对。",
              "- 软件与存储：核对驱动/CANN/容器兼容、进程占用、磁盘布局及 SMART，不自动改配置。",
              "- 本次未执行 HBM 压力、HCCL 微基准、fio 或模型生成，不能把未测试项标为通过。"]
    (out / "report.md").write_text("\n".join(lines) + "\n")


def collect(args):
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=False)
    devices = devices_arg(args.devices)
    expected = devices_arg(args.expected_roce_devices)
    script = collector_script(devices, args.command_timeout)
    host = "local" if args.local else safe_host(args.host)
    command = ["bash", "-s"] if args.local else ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if not args.local:
        if args.jump:
            command += ["-J", safe_host(args.jump)]
        command += [host, "bash", "-s"]
    captured = now()
    try:
        proc = subprocess.run(command, input=script, capture_output=True, text=True, timeout=args.maximum_seconds)
        raw, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr, rc = "overall collection timeout; incomplete snapshot", 124
    (out / "raw.log").write_text(raw)
    (out / "transport.log").write_text(stderr)
    snapshot = {"host": host, "captured_at": captured, "transport_returncode": rc,
                "collector_sha256": hashlib.sha256(script.encode()).hexdigest(), "sections": parse_sections(raw),
                "requested_devices": devices, "expected_roce_devices": expected,
                "read_only": True}
    save(out / "snapshot.json", snapshot)
    result = analyze(snapshot, expected)
    save(out / "findings.json", result)
    report(out, result)
    print(json.dumps({"host": host, "cards": len(result["cards"]), "findings": len(result["findings"]),
                      "transport_returncode": rc, "report": str(out / "report.md")}, ensure_ascii=False))
    return 0 if rc == 0 and result["cards"] else 2


def compare(args):
    before = json.loads((args.before / "snapshot.json").read_text())
    after = json.loads((args.after / "snapshot.json").read_text())
    if before["host"] != after["host"]:
        raise ValueError("host identity differs; do not compare unrelated servers")
    old = analyze(before)["cards"]
    new = analyze(after)["cards"]
    changes = []
    for i in sorted(set(old) | set(new), key=int):
        a, b = old.get(i, {}), new.get(i, {})
        ida = {k: v for k, v in a.get("identity", {}).items() if k in {"VDie ID", "Serial Number", "Serial No", "Serial No."}}
        idb = {k: v for k, v in b.get("identity", {}).items() if k in {"VDie ID", "Serial Number", "Serial No", "Serial No."}}
        identity_changed = any(ida[k] != idb[k] for k in ida.keys() & idb.keys())
        changes.append({"physical_card": i, "identity_changed": identity_changed,
                        "identity_comparison_available": bool(ida.keys() & idb.keys()),
                        "before_link": a.get("roce_link"), "after_link": b.get("roce_link"),
                        "ecc_delta_same_reported_identity_only": None if identity_changed or not (ida.keys() & idb.keys()) else
                        {k: b["ecc"][k] - a["ecc"][k] for k in a.get("ecc", {}).keys() & b.get("ecc", {}).keys()}})
    args.output_dir.mkdir(parents=True, exist_ok=False)
    save(args.output_dir / "comparison.json", {"host": before["host"], "before": before["captured_at"], "after": after["captured_at"], "changes": changes})
    lines = ["# 当前快照对比", "", "| 物理卡 | 身份变化 | 链路前 → 后 |", "| --- | --- | --- |"]
    for c in changes:
        status = "是" if c["identity_changed"] else ("未见变化" if c["identity_comparison_available"] else "身份数据不足")
        lines.append(f"| {c['physical_card']} | {status} | {c['before_link'] or '未测得'} → {c['after_link'] or '未测得'} |")
    lines += ["", "负 ECC 差值可能是清零/复位；身份变化时不计算增量。当前快照对比不能代替显存、通信和业务复测。"]
    (args.output_dir / "report.md").write_text("\n".join(lines) + "\n")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("collect", help="live SSH/local read-only collection, no stress tests or repairs")
    target = a.add_mutually_exclusive_group(required=True)
    target.add_argument("--host")
    target.add_argument("--local", action="store_true")
    a.add_argument("--jump")
    a.add_argument("--devices")
    a.add_argument("--expected-roce-devices")
    a.add_argument("--command-timeout", type=int, default=12)
    a.add_argument("--maximum-seconds", type=int, default=240)
    a.add_argument("--output-dir", type=Path, required=True)
    a = sub.add_parser("compare")
    a.add_argument("--before", type=Path, required=True)
    a.add_argument("--after", type=Path, required=True)
    a.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    if args.command == "collect":
        if args.command_timeout <= 0 or args.maximum_seconds <= 0:
            p.error("timeouts must be positive")
        if args.local and sys.platform != "linux":
            p.error("--local requires the actual Linux target, not a Mac")
        return collect(args)
    return compare(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(2)
