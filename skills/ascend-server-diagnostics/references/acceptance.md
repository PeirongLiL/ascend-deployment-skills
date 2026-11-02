# 深入验证：只在确认授权与设备空闲后执行

## HBM

`hbm_check.py` 在具备 torch/torch_npu 的目标运行环境运行。明确物理卡到容器逻辑编号映射；不默认重启业务腾出显存，不默认启动 privileged 容器。

先采 ECC/健康与进程占用；执行 `python3 hbm_check.py --device LOGICAL_ID --max-gib SIZE --reserve-gib 8 --passes 3 --max-seconds 180 --execute`；再采 ECC 和内核日志。SIZE 按卡容量与可用显存选择，不能把历史 52 GiB 固定应用到所有卡。任何不可纠正 ECC、数据不一致或运行异常立即终止并收集证据。

脚本用分块独立已知模式、CPU 回读校验与小规模已知结果矩阵运算。超时/不足目标容量是未完成，不是通过；单次通过也不是穷尽式硬件认证。重启后当前 ECC 为零而累计错误仍存在时，需要解释身份、时间与故障历史。

## 通信

`hccl_microbench.py` 是对既有 Ascend 分布式微基准的封装资源，依赖目标 torch_npu 与 torchrun。先确认所有目标设备空闲、真实数据面 IP/路由与维护窗口，再按单机→健康节点配对→小规模→必要时全规模逐级运行。

示例单机：`torchrun --standalone --nproc_per_node=8 hccl_microbench.py --ops all_reduce,all_gather,reduce_scatter,all_to_all --sizes 4K,1M,16M --warmup 3 --iters 5 --output NEW_DIR/results.jsonl --execute`。跨机使用当前互通 rendezvous 地址，每节点不同 node_rank；不可照搬旧 rank/IP。启动所有 rank 后设整体超时，异常只清理本轮 PID。

采集测试前后 `hccn_tool`/ethtool 计数，检查实际参与端口、错误增量、rank 最大值/中位数与数据正确性。一次异常先复现并换健康对照配对，不直接认定网卡坏。PFC/CNP 不等同于物理坏链路。

## 存储与软件

当前快照记录 lsblk、LVM/MD、文件系统、NVMe 健康和 PCIe 信息；没有安装相关工具时报告缺失，不自动安装或认定磁盘健康。

只读 fio 必须指定已有普通测试文件、文件大小、direct I/O、块大小、队列深度和预算；不要向生产裸设备写，不因“修复磁盘慢”自动格式化或重组 RAID。记录页缓存条件与 GB/s/GiB/s。

## 修复复核

前后快照的采集主机与硬件身份必须匹配；更换卡则按同一插槽的新身份单独验收。对累计计数、隔离页与链路比较要结合时间和设备身份。业务验收需另行完成 health/models/真实推理；不能仅凭卡或链路通过宣称模型服务恢复。
