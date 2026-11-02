# 在线测试协议与输出口径

`benchmark.py` 仅依赖 Python 标准库。`run` 必须带 `--execute`，`plan` 不联网，`inspect` 只 GET。API key 从配置指定的环境变量读取，缺省不鉴权；不把鉴权值写入清单。禁止 URL 内嵌用户名、密码、query token，禁止配置里存明文 key。

## 在线构造与预热

- `completions` 用 `/tokenize` 取得种子 token，构造长度严格等于输入目标的 token ID 列表；正式响应 usage 再次校验。服务不接受 token ID prompt 时不要悄悄换成近似输入长度，改用支持的 tokenizer/文本构造方式并验证。
- `chat` 需要 `prompt_text`，可用 `expected_prompt_tokens` 校验包含模板的 usage；仅有目标 `input_tokens` 时也会严格核验。没有 `/tokenize` 的网关可以使用 chat 文本，但不能伪称已完成 tokenizer 预检。
- `isolated` 为每个请求生成唯一 cache salt；适用于明确支持该参数的 vLLM 服务。配置 `cache_salt_supported=true` 表示已确认支持；否则 `run` 拒绝执行 isolated 场景。`shared_prefix` 共用精确比例的前缀，按 `shared_prefix_ratio` 构造，并差分采集指标。
- 预热使用配置中的输出长度，不能用固定 32 token 代表所有 decode 路径。脚本要求预热全部成功，但这仍不证明所有 DP rank 热稳；结合服务日志和时延波动决定是否增加预热。
- `maximum_runtime_seconds` 是整轮墙钟预算，包含预检/构造/预热和正式测试。到期停止调度，已经开始的请求也受剩余预算限制。每个用例失败后不会自动重启服务或扩大资源。

## 指标

- 成功：HTTP 200、收到结束标记、usage 合法且与输入条件一致、输出非空；强制长度用例还要求输出 token 严格匹配且 finish_reason 为 length。
- TTFT：收到第一个非空正文或 reasoning 内容的时间减发送时间。TTFC：收到第一个非空正文内容的时间减发送时间；reasoning-only 不编造 TTFC。
- TPOT：(最后有效输出时间 − 首个有效输出时间) / (输出 token 数 − 1)，只有输出至少 2 token 且时间可用时才计算。E2E 到完整响应流结束，因此 TPOT 与 E2E 的收尾口径不同。
- 输出吞吐：成功请求输出 token 总数 / 整个正式用例墙钟时间；总 token 吞吐用输入加输出。失败请求消耗的时间保留在分母中。
- 平均吞吐：每个成功请求的输出 token/E2E 的算术平均，不等于系统总吞吐；平均单请求解码速度另列。
- Goodput：同时满足所有配置 SLO 的成功请求数 / 正式墙钟时间。达标比例分别对全部请求与成功请求计算。未设 SLO 时为空；样本不足时保留分位数但标注，不以 P99 宣称稳定长尾。
- SSE chunk 可含多个 token，脚本不输出伪造 ITL。需 ITL 时用当前版本支持的 `vllm bench serve` 或服务 token 级 tracing，标明具体工具/版本/定义。
- `/metrics` 是服务全局计数。并发其他业务、计数器重置或采集缺失会影响缓存差分；无法归属本轮时不要将它解读为本轮精确命中率。

## 输出与 Excel

每个输出目录必须新建。`manifest.json` 保存脱敏配置、脚本 SHA256、运行时间、当前模型列表；`requests.jsonl` 包含预热及正式请求，按 phase 区分。`summary.json` 包含每个用例、覆盖/完成状态和标准中文列。

导出脚本接受两个位置参数：`node scripts/export_xlsx.mjs summary.json report.xlsx`，可选 `--preview`。保持 29 列顺序：模型、并行度、提示长度、输出长度、测试总时长、请求计数、吞吐/QPS/Goodput、平均与分位时延、实际 token、API URL、温度和流式输出。额外运行条件在“运行条件”页，不把未知值填成零。

在 Codex 中使用依赖定位工具取得受支持 Node 与 node_modules 路径，再在 Skill 的 scripts 上级创建 `node_modules` 软链接。不可用时明确提示，不自动安装。新建/修改工作簿时遵循当前 spreadsheet 技能的 authoring marker、检查与渲染约定；不要在 Skill 中固定某一台 Mac 的依赖路径。

## 可选：vLLM 原生 bench

若用户明确要求原生 bench 或需要 ITL，先在服务所在镜像执行 `vllm bench serve --help` 核对该版本参数。示例参数：backend=openai-chat、endpoint=/v1/chat/completions、dataset-name=random、random-input-len、random-output-len、max-concurrency、num-prompts、temperature=0、ignore-eos、seed=42、percentile-metrics=ttft,tpot,itl,e2el。当前版本支持 JSON/请求级结果保存时启用，保留完整命令；不能只截屏抄平均数。其随机长度/模板与本脚本精确 raw-token 协议可能不同，先核对实际长度再比较。
