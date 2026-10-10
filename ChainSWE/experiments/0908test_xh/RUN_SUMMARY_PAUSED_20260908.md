# 0908 XH 正式实验暂停总结

更新时间：2026-09-08 14:04（Asia/Shanghai）

实验目录：`/lg-home/cog8/lwli6/ChainSweExp/ChainSWE/experiments/0908test_xh`

状态：**PAUSED**。tmux session 已关闭，5 个属于本实验的 Docker 容器已停止，宿主机上没有本实验的 launcher 或 `run_experiment.py` 进程，当前不再消耗模型 API。已有文件和停止的容器均保留。

## 1. 实验目标与配置

本实验参考 `experiments/0907test_xh_on30`，计划对严格 F2P/P2P 通过的 30 条 chain 进行正式评测：

- 模型：`spark-x2.5-Code-PT-SWE-Pro-o-66`
- 模型别名：`xh-model`
- API：OpenAI-compatible Chat Completions，`USE_RESPONSES_API=false`
- agent：`baseline`
- reasoning effort：`medium`
- 模式：`oracle`、`seq`、`seq_mem`
- 并发：5 条 chain
- 计划规模：30 chains × 3 modes × 3 stages = 270 stage executions
- agent timeout：1800 秒
- test timeout：1800 秒
- bridge 外层等待：agent timeout + 120 秒，即 1920 秒

启动时实际使用的配置见 [launcher_config.json](output/launcher_config.json)，chain 清单见 [chain_list_30.txt](chain_list_30.txt)，模型配置见 [models_xh.json](models_xh.json)。

## 2. 本次运行前已经完成或纳入的改动

### 2.1 模型可见工具名调整

为规避 Spark 模型对旧 OpenHands 风格工具名的文本协议偏置，本次 wheel 对模型暴露的工具名为：

| 旧名称 | 当前名称 |
|---|---|
| `execute_bash` | `run_shell` |
| `str_replace_editor` | `file_editor` |
| `finish` | `finish` |

正式运行产生的 `tools.jsonl` 和成功的原生调用记录均确认 schema 中使用的是 `run_shell`、`file_editor`、`finish`。本次使用的 SWE-Edit wheel SHA256 为：

```text
b4d57a02da26be5edfaf93994ad13175d5f57561ffd71dfb93392473e8f9ade3
```

### 2.2 shell 环境激活兼容

`run_shell` 不再无条件 source `/opt/miniconda3`，而是依次检查：

1. `/opt/miniconda3/etc/profile.d/conda.sh`
2. `/opt/conda/etc/profile.d/conda.sh`
3. `$HOME/miniconda3/etc/profile.d/conda.sh`
4. 都不存在时继续执行，不因缺少固定 Conda 路径直接失败

### 2.3 0908 实验脚手架

本次新增了：

- [run_in_tmux.sh](run_in_tmux.sh)：固定 30 chains、3 modes、5 路并发并自动汇总
- [models_xh.json](models_xh.json)：XH 模型及 Chat Completions 配置
- [chain_list_30.txt](chain_list_30.txt)：本次固定 chain 清单
- [README.md](README.md)：启动、监控和汇总命令
- `scripts/tool/monitor_0908_xh.py`：实时 stage 监控
- `scripts/tool/summarize_0908_xh.py`：实验结束后的指标汇总

实验启动前 dry-run 已通过，确认生成 90 个 chain-mode task、270 个计划 stage，`parallel_chains=5`。

### 2.4 尚未完成的关键修复

工具改名已经生效，但以下机制尚未实现：

- native `tool_calls` 缺失时的同轮重试
- 文本伪工具标签检测与丢弃
- 协议漂移后的上下文回滚/重置
- 连续无有效工具调用的快速失败
- 每次 API 请求的开始、结束、retry 和 request ID 观测

因此，改名降低了触发旧协议的概率，但没有消除多轮运行中的协议漂移。

## 3. 暂停时结果快照

暂停后等待在途写入结束，再次读取结果得到以下静态快照：

| 指标 | 数量 |
|---|---:|
| 计划 stages | 270 |
| 已生成 `result.json` | 26（9.63%） |
| `RESOLVED` | 2（已完成结果中的 7.69%） |
| `AGENT_TIMEOUT` | 16 |
| `INFRA_ERROR` | 4 |
| `TEST_COMMAND_FAILED` | 4 |
| 完整成功 chain | 0 |
| 已开始 chain | 5 |
| 尚未开始 chain | 25 |

模式进度：

- `oracle`：首批 5 条 chain 的 15 个 stage 都已产生结果；其中 2 个 chain-mode 被标记 complete，3 个因 infra 不完整。
- `seq`：产生了 11 个 stage 结果；1 个 chain-mode 完成，4 个仍为 partial。
- `seq_mem`：只有 WeasyPrint 创建了第 1 个 stage 目录，暂停时尚未产生 stage result。

注意：monitor 中的 `oracle:complete` 只表示该 chain-mode 的 3 个 stage 都处理完，不表示 3 个 bug 全部修复成功。

## 4. 已 RESOLVED 的 stages

本次共有两个 stage 的官方测试命令返回 0。两者的 `agent_status` 都是 `timeout`，但 agent 在超时前已留下有效补丁，随后官方测试通过，因此最终 `failure_type` 被正确判为 `RESOLVED`。

### 4.1 WeasyPrint / seq / position 1

- chain：`Kozea_WeasyPrint_20240613`
- instance：`kozea__weasyprint-2058`
- mode：`seq`
- model patch：6002 bytes
- 官方测试：`tests/draw/test_table.py::test_tables_24_rtl`，rc=0
- stage 目录：

```text
/lg-home/cog8/lwli6/ChainSweExp/ChainSWE/experiments/0908test_xh/output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/seq/stages/01_kozea__weasyprint-2058
```

关键文件：

- [result.json](output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/seq/stages/01_kozea__weasyprint-2058/result.json)
- [trajectory/traj.jsonl](output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/seq/stages/01_kozea__weasyprint-2058/trajectory/traj.jsonl)
- [model.patch](output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/seq/stages/01_kozea__weasyprint-2058/model.patch)
- [test.log](output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/seq/stages/01_kozea__weasyprint-2058/test.log)

### 4.2 MONAI / oracle / position 2

- chain：`Project-MONAI_MONAI_20220715`
- instance：`Project-MONAI__MONAI-4716`
- mode：`oracle`
- model patch：22406 bytes
- 官方测试：`tests/test_meta_tensor.py`，rc=0
- stage 目录：

```text
/lg-home/cog8/lwli6/ChainSweExp/ChainSWE/experiments/0908test_xh/output/chain_runs/Project-MONAI_MONAI_20220715/chains/Project-MONAI_MONAI_20220715/oracle/stages/02_Project-MONAI__MONAI-4716
```

关键文件：

- [result.json](output/chain_runs/Project-MONAI_MONAI_20220715/chains/Project-MONAI_MONAI_20220715/oracle/stages/02_Project-MONAI__MONAI-4716/result.json)
- [trajectory/traj.jsonl](output/chain_runs/Project-MONAI_MONAI_20220715/chains/Project-MONAI_MONAI_20220715/oracle/stages/02_Project-MONAI__MONAI-4716/trajectory/traj.jsonl)
- [model.patch](output/chain_runs/Project-MONAI_MONAI_20220715/chains/Project-MONAI_MONAI_20220715/oracle/stages/02_Project-MONAI__MONAI-4716/model.patch)
- [test.log](output/chain_runs/Project-MONAI_MONAI_20220715/chains/Project-MONAI_MONAI_20220715/oracle/stages/02_Project-MONAI__MONAI-4716/test.log)

## 5. 主要失败类型

### 5.1 AGENT_TIMEOUT：主要由工具协议漂移造成

当前 16 个 `AGENT_TIMEOUT` 可按轨迹分为：

```text
AGENT_TIMEOUT (16)
├── TOOL_PROTOCOL_DRIFT (14)
├── VALID_AGENT_LOOP_TIMEOUT (2)
├── API_STALL (0 confirmed)
└── UNKNOWN_TIMEOUT (0)
```

`TOOL_PROTOCOL_DRIFT` 的典型过程是：

```text
模型把调用写成 <run_shell...>、<function=...> 或 Markdown 命令
→ Chat Completions message.tool_calls 为空
→ agent 将这段文本加入历史
→ agent 注入 “If you have completed...”
→ 模型继续模仿文本协议
→ 循环到 1800 秒
```

14 个协议漂移案例中既有首轮即漂移，也有先完成大量原生调用、后期才漂移。这说明只做“首轮重试”仍不充分，保护逻辑需要覆盖每一轮。

`VALID_AGENT_LOOP_TIMEOUT` 的两个案例都来自 `Project-MONAI_MONAI_20220715` 的 oracle：position 1 有 68 次原生工具调用，position 3 有 43 次。它们超时前仍在执行有效 `run_shell`/`file_editor`，属于没有在预算内收敛或调用 `finish`。

现有 token 统计全部显示 `api_rounds=0` 和 tokens=0，与轨迹中的大量真实请求矛盾，因此不能据此判断 API 是否 stall。当前也缺少 per-request 时间戳，所以没有把任何案例确定归类为 `API_STALL`。

### 5.2 INFRA_ERROR：bridge 外层超时

4 个 `INFRA_ERROR` 的错误完全相同：

```text
TimeoutError: Agent bridge did not respond within 1920s
```

这些 stage 没有可用 trajectory；bridge log 显示依赖安装完成，但宿主 runner 在 1920 秒内没有收到 `CHAINSWE_AGENT_RESULT=`。因此它们是基础设施/bridge 层失败，不应计作模型补丁失败。现有日志不足以继续区分内部 API stall、agent 卡死或 bridge 输出丢失。

### 5.3 TEST_COMMAND_FAILED

4 个 stage 正常走到了官方测试，但测试命令返回非零：

- Hypothesis oracle position 2：产生 7134-byte patch，测试失败
- WeasyPrint oracle position 2：产生 2767-byte patch，测试失败
- WeasyPrint oracle position 3：`model.patch` 为 0，测试失败
- MONAI 202205 seq position 1：`model.patch` 为 0，测试失败

这类失败与 infra 不同，说明评测链路工作正常，但模型修改缺失或修改不正确。

## 6. 典型失误轨迹和诊断路径

### 6.1 首轮工具协议漂移：0 次原生调用

Hypothesis seq position 1 从开始就输出文本 `<run_shell>`，原生 `run_shell`/`file_editor`/`finish` 计数全部为 0，随后不断收到脚手架注入的继续提示。

- [典型轨迹](output/chain_runs/HypothesisWorks_hypothesis_20210304/chains/HypothesisWorks_hypothesis_20210304/seq/stages/01_hypothesisworks__hypothesis-2889/trajectory/traj.jsonl)
- [result.json](output/chain_runs/HypothesisWorks_hypothesis_20210304/chains/HypothesisWorks_hypothesis_20210304/seq/stages/01_hypothesisworks__hypothesis-2889/result.json)

### 6.2 后期协议漂移：先正常调用，再锁死

Hypothesis oracle position 3 先完成 86 次原生工具调用，后期开始输出 `<function=finish>` 和不存在的文本工具名，最终未产生原生 `finish` 并超时。

- [典型轨迹](output/chain_runs/HypothesisWorks_hypothesis_20210304/chains/HypothesisWorks_hypothesis_20210304/oracle/stages/03_hypothesisworks__hypothesis-2924/trajectory/traj.jsonl)
- [result.json](output/chain_runs/HypothesisWorks_hypothesis_20210304/chains/HypothesisWorks_hypothesis_20210304/oracle/stages/03_hypothesisworks__hypothesis-2924/result.json)

### 6.3 文本 finish 无法终止 agent

pynwb oracle position 2 多次输出文本 `<function=finish>`，但没有形成 native `finish` tool call；agent 把它视为普通文本并继续循环。

- [典型轨迹](output/chain_runs/NeurodataWithoutBorders_pynwb_20250320/chains/NeurodataWithoutBorders_pynwb_20250320/oracle/stages/02_NeurodataWithoutBorders__pynwb-2056/trajectory/traj.jsonl)
- [result.json](output/chain_runs/NeurodataWithoutBorders_pynwb_20250320/chains/NeurodataWithoutBorders_pynwb_20250320/oracle/stages/02_NeurodataWithoutBorders__pynwb-2056/result.json)

### 6.4 正常工具循环但未收敛

MONAI 202207 oracle position 1 全程维持 native tool call，累计 68 次调用，超时前仍在调试 `ForkingPickler`，没有协议漂移，但没有在 1800 秒内完成。

- [典型轨迹](output/chain_runs/Project-MONAI_MONAI_20220715/chains/Project-MONAI_MONAI_20220715/oracle/stages/01_Project-MONAI__MONAI-4709/trajectory/traj.jsonl)
- [result.json](output/chain_runs/Project-MONAI_MONAI_20220715/chains/Project-MONAI_MONAI_20220715/oracle/stages/01_Project-MONAI__MONAI-4709/result.json)

### 6.5 bridge 无响应，没有 trajectory

Hypothesis oracle position 1 是典型 infra case。stage 只有准备文件、bridge log 和错误结果，没有 agent trajectory。

- [agent_bridge.log](output/chain_runs/HypothesisWorks_hypothesis_20210304/chains/HypothesisWorks_hypothesis_20210304/oracle/stages/01_hypothesisworks__hypothesis-2889/agent_bridge.log)
- [result.json](output/chain_runs/HypothesisWorks_hypothesis_20210304/chains/HypothesisWorks_hypothesis_20210304/oracle/stages/01_hypothesisworks__hypothesis-2889/result.json)

### 6.6 agent 无修改结束，官方测试失败

WeasyPrint oracle position 3 的 `model.patch` 为 0，agent 没有留下有效修改，官方测试随后失败。

- [典型轨迹](output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/oracle/stages/03_kozea__weasyprint-2231/trajectory/traj.jsonl)
- [result.json](output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/oracle/stages/03_kozea__weasyprint-2231/result.json)
- [test.log](output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/oracle/stages/03_kozea__weasyprint-2231/test.log)

## 7. 继续实验的建议

### 优先级 P0：先修协议漂移，不直接继续 30-chain 正式跑

当前 16 个 agent timeout 中 14 个由协议漂移主导。直接提高 agent timeout 只会让错误循环持续更久，不会提高成功率。建议在 SWE-Edit agent loop 中加入：

1. 当 `tool_calls` 为空但 `message.content` 含 `<run_shell`、`<file_editor`、`<finish`、`<function=`、`<tool_call>` 或明显 Markdown 工具命令时，标记 `TOOL_PROTOCOL_DRIFT`。
2. 不把该 assistant 文本加入长期对话历史；回滚本轮并以相同干净上下文重试。
3. 每轮最多重试 3–5 次，而不只保护首轮，因为本次存在多例后期漂移。
4. 连续两轮或三轮没有 native tool call 时快速终止并记录细分类，避免消耗完整 1800 秒。
5. 不建议直接执行从损坏文本解析出的 shell 命令；网关输出存在标签残缺，自动兜底执行风险较高。

### 优先级 P0：增加可判定的 API/bridge 观测

每次 LLM 请求至少记录：开始时间、结束时间、耗时、attempt、HTTP/SDK error、request ID、finish reason、native tool call 数量。bridge 每隔固定时间写 heartbeat。这样才能可靠区分：

```text
AGENT_TIMEOUT
├── TOOL_PROTOCOL_DRIFT
├── VALID_AGENT_LOOP_TIMEOUT
├── API_STALL
└── UNKNOWN_TIMEOUT
```

同时修复 Chat Completions usage 字段映射；当前 `api_rounds=0`、token 全 0 明显不是实际值。

### 优先级 P1：为正常长循环增加收敛控制

对仍在正常调用工具的 agent：

- 设置迭代数或工具调用数软预算，在接近 1800 秒时提示总结、运行目标测试并调用 `finish`
- 对重复命令、重复查看同一文件、重复失败编辑设置检测
- 将“测试已通过但未调用 finish”作为可提前结束条件
- 暂不整体提高 1800 秒上限；先消除协议循环后再评估是否只有少数真实复杂任务需要更长预算

### 优先级 P1：先 smoke gate，再恢复正式并发

修复后先选择本报告中的代表性 stage 做重复 smoke：

- 首轮漂移：Hypothesis seq position 1
- 后期漂移：Hypothesis oracle position 3
- 文本 finish：pynwb oracle position 2
- 正常长循环：MONAI 202207 oracle position 1
- 已成功基线：WeasyPrint seq position 1

建议每个 case 重复至少 3 次。进入正式运行前至少确认：首轮 native tool call 稳定、全程无文本协议锁死、无 bridge timeout、轨迹和 usage 可观测。

### 优先级 P1：修改 wheel 后必须新建 run

`run_experiment.py` 的科学配置身份包含 wheel SHA256 和 agent server SHA256。若修改 SWE-Edit、工具协议保护或 agent bridge，不能在当前 `output/` 上直接 `--resume`，否则配置身份不一致，且会混合两个实现版本的结果。

建议新建：

```text
experiments/0908test_xh/output_v2/
```

并在新 run 的 README/launcher config 中记录新 wheel hash、agent server hash和修复说明。当前 `output/` 应保持冻结，作为失败诊断与前后对照基线。

只有在 wheel、agent server、模型配置和数据完全不变，且只是希望重试瞬时 infra failure 时，才适合对当前 run 使用 `--resume`。当前整体问题是实现级协议漂移，因此推荐修复后开新 run，而不是原地续跑。

## 8. 暂停状态与恢复注意事项

- 暂停标记：[output/PAUSED](output/PAUSED)
- 原 tmux session：`chainswe_0908_xh30_p5`，已不存在
- 5 个容器已 stop、未删除，便于诊断
- 顶层 launcher 未完成，因此没有最终 aggregate 报告
- 不要直接再次执行现有 `run_in_tmux.sh`；当前 `output/` 已存在，默认 launcher 会拒绝覆盖
- 若仅按原版本续跑，需要单独准备带 `--resume` 的启动入口
- 若按建议修改协议处理逻辑，则应建立 `output_v2`，不要复用当前结果目录

## 9. 当前结论

工具改名和 shell 环境兼容已经生效，并且得到两个真实通过官方测试的 stage，证明完整评测链路能够工作。然而当前 26 个已完成 stage 中仅 2 个 resolved，16 个 agent timeout 中有 14 个明确受到文本工具协议漂移影响，另有 4 个 bridge timeout。该失败分布说明当前瓶颈是模型—脚手架工具协议稳定性和超时可观测性，而不是单纯的任务难度或 timeout 数值。

因此，本轮最有价值的产出是确认了：工具改名不是充分修复；协议保护必须覆盖每轮、必须避免把错误文本写回历史，并应在修复后通过小规模重复 smoke，再新建版本隔离的正式 run。
