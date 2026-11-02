---
name: ascend-server-diagnostics
description: "通过 SSH 实时检查新装昇腾服务器或异常节点的 NPU、HBM/ECC、RoCE/HCCL、磁盘与运行环境，定位故障并生成证据报告；支持授权后的显存和通信验证。"
---

# 昇腾服务器实时验收与故障诊断

直接检查用户指定的当前服务器。历史案例用于选择排查路径，不作为当前故障结论。

## 实际操作

1. 确认主机、异常现象、是否运行生产服务。读取已有 SSH 别名，直接连接优先；用户要求跳板时用 `--jump`。不永久改连接配置，不复制私钥。
2. 执行 `python3 scripts/diagnose.py collect --host HOST --output-dir NEW_DIR`。脚本通过 SSH 只读采集系统、驱动、NPU 健康/ECC、逐卡 RoCE、主机网口、存储和内核日志，输出当前报告与逐项原始证据。设备自动发现失败时补充 `--devices 0,1,...`；未发现设备不能当成检查通过。
3. 阅读 `report.md`、`findings.json`，根据故障域进一步查证。脚本判定是初筛，不是替代现场推理：观察时间、物理设备映射、错误是否在当前窗口出现，区别存量计数与增量。
4. 需要深入验证时读 [references/acceptance.md](references/acceptance.md)。确认空闲卡/维护窗口与用户测试范围后再运行 `scripts/hbm_check.py` 或 `scripts/hccl_microbench.py`。默认巡检不分配大量显存、不跑 fio、不重置设备。
5. 修复后用新的输出目录重新采集，用 `compare --before OLD_DIR --after NEW_DIR --output-dir NEW_DIR2` 比较身份、计数器与链路。报告分别注明单卡、整机、跨机及业务验收覆盖程度；只有实际完成的层级才能标通过。

脚本路径相对于本 Skill 目录。若 SSH 失败，保留连接失败证据并报告无法检查，不输出健康结论。`--local` 仅用于确实在目标 Linux 主机上运行的采集，不能将 Mac 本地状态称为远端状态。

## 诊断重点

- **NPU/HBM：**物理卡号、PCIe、VDie/序列号与容器逻辑编号映射；当前和累计 ECC、隔离/待隔离页。健康 OK 或复位后计数清零不足以证明硬件可靠。单卡显存校验通过不等于八卡模型部署验收通过。
- **RoCE/HCCL：**区分管理网、主机业务网与 NPU 数据网。逐卡链路、net health、光模块状态和对照端口；NPU 健康 OK 仍可能链路 DOWN。确认实际使用的端口后再定性，未使用端口 DOWN 不自动视为业务故障。
- **通信慢：**端口错误计数增量、不同消息尺寸 collective、故障节点与健康对照节点配对。一次慢 rank 或低有效带宽不能直接证明网卡坏。不要把 bond 带宽当成所有 NPU 的数据面上限。
- **存储/环境：**NVMe/SMART、PCIe、RAID/LVM、文件系统、CPU/NUMA、共享内存、驱动/CANN/容器兼容和进程占用。布局不合理与硬件损坏分开；读取模型慢先定位再修复。
- **证据等级：**确认异常、疑似异常、当前样本未见异常、未测试。命令失败、权限不足或工具缺失必须标未测试，不能当正常。输出原始命令、退出码、时间及行号。

## 权限边界与输出

`collect` 和 `compare` 不修改远端配置。压力验证、设备复位、服务重启、驱动升级、网口重配、换卡、RAID 重建需要各自符合当前用户授权，不能因为“检查服务器”自动执行。

每次输出 `snapshot.json`、`raw.log`、`findings.json`、`report.md`，便于复核、厂商报修和真实演示。需要历史背景时再读 [references/cases.md](references/cases.md)，不要将案例里的主机角色和指标当成当前值。

## 使用示例

“检查新装的 ascend-node01，先做只读巡检，看看显卡和高速网口有没有异常。”

“当前模型在 HCCL 初始化卡住，检查指定八台服务器的通信链路，先不要重启服务。”
