---
name: ascend-llm-benchmark
description: "直接连接 Kimi、DeepSeek 等 OpenAI 兼容模型服务，执行实时性能测评、并发与长上下文测试、缓存场景对比，输出标准 Excel、请求级记录和可复现报告。"
---

# 昇腾模型服务性能测评

主流程是检查当前服务并实际发出测试请求，不是检索旧日志。也适用于其他 OpenAI 兼容推理服务；Ascend 配置核验由 SSH 和服务日志补充。

## 实际操作

1. 明确在线服务地址、模型、输入/输出 token、并发、请求数、缓存条件与时间预算。沿用用户指定数值，不擅自改成历史配置。缺少入口时从当前 SSH 别名和服务监听端口只读查找，密钥不写入文件。
2. 用 `scripts/benchmark.py inspect --base-url URL` 查询当前 `/health`、`/v1/models`、`/metrics`。模型多于一个时明确选择；不能只凭 HTTP 200 宣称所有 DP rank 已热稳。内网入口可用 `ssh -N -L` 临时隧道，不永久改 SSH 配置。
3. 从 `assets/kimi-8k-1k.json` 或 `assets/deepseek-curves.json` 复制配置到任务目录，填写当前入口、模型、配置快照及输出目录；读 [references/protocol.md](references/protocol.md) 核对统计口径。
4. 先运行 `python3 scripts/benchmark.py plan --config CONFIG`，展示工况数量、正式/预热请求量、预算和缓存条件。用户已要求压测且参数明确即可运行 `run --config CONFIG --output-dir NEW_DIR --execute`；仅问现状或原因不授权压测。服务重启、关闭 Prefix Cache、清页缓存都不在脚本权限内。
5. 实际运行会在线 token 校验、预热并检验成功、记录 SSE 和 usage，遇到预算耗尽、预热失败、连续请求失败会停止。失败/未完成工况不能当作完成结果。正式热态基线建议相同协议 3 次，保留每次结果及波动；快速单次测试明确标注。
6. 输出 `summary.json`、`requests.jsonl`、`manifest.json`、`report.md`。调用 `scripts/export_xlsx.mjs SUMMARY OUTPUT.xlsx` 生成标准报表；该脚本需要 `@oai/artifact-tool`，使用当前环境提供的运行时，不安装任意替代依赖。也可用 `summarize` 从本次请求记录重算。

命令中的 `scripts/`、`assets/` 均相对于本 Skill 目录。导出前建立指向受支持 Node 依赖目录的 `node_modules` 链接；具体环境定位见 [references/protocol.md](references/protocol.md)。

## 必须保留的边界

- 明确测试的是模型直连、网关还是应用全链路。记录模型、镜像/框架、量化、DP/TP/EP、上下文、MTP、采样参数及脚本哈希；未知字段标未知，不根据历史角色补成当前配置。
- raw completions 支持在线精确输入 token 构造。chat 的输入含模板开销，实测 usage 必须单独核验；不承诺“文本相同字数等于相同 token”。
- `cache_mode=isolated` 使用每轮唯一 salt 隔离跨请求前缀复用，但不等于关闭服务 Prefix Cache；只有确认服务支持 salt 且命中指标合理才如此解释。共享前缀实验记录设计比例和实测命中差分，缺失指标则不报告实测命中率。
- 单请求解码速度、系统输出吞吐、输入加输出吞吐各自报告。SSE chunk 不一定是一个 token，不伪造 ITL。Goodput 必须附 SLO 和分母；没有 SLO 时留空，不能填零。
- `max_tokens` 强制长度性能用例和正常结束的功能用例分开。旧请求的时延口径可能包含协议收尾，比较前核实。
- 冷启动是独立任务：读 [references/startup.md](references/startup.md)，保持计时起止与缓存条件一致。不能把权重子阶段与完整模型加载重复相加。
- 不覆盖 baseline；不输出鉴权值，不自动修改服务参数或卸载当前模型。对外报告只声明已完成且有原始记录的用例。

## 使用示例

“测当前 Kimi 服务，输入 8K、每请求输出 1K、并发 4、16 请求，生成标准 Excel。”

“连接当前 DeepSeek 服务，先检查配置，再扫描并发 1、4、8；总时长不超过 10 分钟。”
