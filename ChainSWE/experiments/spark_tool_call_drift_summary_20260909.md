# Spark 工具调用乱码与协议漂移排查总结

日期：2026-09-09  
涉及模型：`spark-x2.5-Code-PT-SWE-Pro-o-66`  
涉及框架：ChainSWE / SWE-Edit（OpenAI Chat Completions 模式）

## 一、结论

本次问题不是单一的“提示词写错”或“SWE-Edit 没有解析正常工具调用”，而是以下因素共同造成：

1. Spark 模型/网关在部分提示词和工具名组合下会概率性偏离 OpenAI 原生工具协议。
2. 漂移时，原始 HTTP 响应中的 `message.tool_calls` 本身为空；工具调用被输出成普通
   `message.content` 文本，并且标签经常残缺或混用多种格式。
3. SWE-Edit 原实现把这条普通 assistant 文本加入后续历史。模型看到错误格式后会继续模仿，
   从一次概率性首轮失败演变成后续多轮持续乱码、始终没有工具执行。
4. 工具改名能明显降低失败概率，但不能从协议层彻底解决问题。


因此，当前应同时采用：

- 使用较稳定的 `run_shell` / `file_editor` 工具名；
- 在客户端加入“无原生 tool call 时保持历史干净并重试”的恢复机制；
- 正式运行时保证容器安装包和实验源码版本一致；
- 最终仍需网关侧修复 tool parser 和 forced tool choice 合约。

## 二、为什么不是 SDK 或轨迹解析丢失

已有 exact replay 和本次复核均表明：

- 成功响应会在原始 JSON 中包含标准 `choices[0].message.tool_calls`，OpenAI SDK 2.7.1
  可以正常解析，SWE-Edit 也可以提取、执行并记录工具调用。
- 失败响应在进入 SDK 前就已经没有 `message.tool_calls`，常见特征为：
  - HTTP 状态为 200；
  - `finish_reason=stop`，或者异常地出现 `finish_reason=tool_calls` 但调用数组仍为空；
  - `message.content` 中出现 `<execute_bash...`、`<tool_call>...`、
    `<function=execute_bash>...` 等文本格式；
  - 标签可能缺少 `>`、闭合标签不匹配，或混用 `arg_key`、`command`、`parameter`。
- 相同 API、模型和工具 schema 在较短提示词下可以返回标准原生调用。

这说明客户端没有把一条正常的结构化调用丢掉。直接故障位于模型生成或网关 tool parser
转换阶段；客户端能做的是隔离失败响应、重试并提供审计证据。

## 三、提示词与工具名的影响

历史消融实验得到以下结论：

- 删除多余换行没有修复故障。
- 删除 prompt 中显式出现的工具名没有修复故障。
- 添加“只允许原生 tool_calls、禁止文本标签”的指令没有稳定修复故障。
- `execute_bash` / `str_replace_editor` 更容易触发模型训练阶段学到的文本工具协议。
- 改成 `run_shell` / `file_editor` 后成功率显著提高，但仍属于概率改善，不是网关协议修复。
- 首轮失败文本一旦进入历史，后续会明显倾向继续使用相同错误格式；正确的原生调用历史则会
  强化正确格式。

所以，提示词会影响触发概率，但不能将问题归结为某一句提示词。真正危险的是“概率性协议漂移
与错误响应进入历史”形成的自我强化。

## 四、已经落地的客户端恢复机制

修改文件：

- `/lg-home/cog8/lwli6/ChainSweExp/SWE-Edit/src/sweedit/core/agent.py`
- `/lg-home/cog8/lwli6/ChainSweExp/SWE-Edit/src/sweedit/core/context_manager.py`
- `/lg-home/cog8/lwli6/ChainSweExp/SWE-Edit/tests/test_tool_drift_recovery.py`

机制行为如下：

1. 仅对带工具的 OpenAI Chat Completions agent 启用，避免改变 Anthropic 和 OpenAI
   Responses API 的原有行为。
2. 每次模型响应后先检查是否存在原生 `message.tool_calls`。
3. 没有原生调用时，不把 assistant 的乱码文本加入 `_messages`，而是使用完全相同的干净请求
   上下文重试。
4. 默认总尝试次数为 5，可通过 `AgentBase(tool_call_retry_attempts=...)` 调整。
5. 被丢弃响应写入独立的 `tool_call_drift.jsonl`，不会混入可回放的主轨迹 `traj.jsonl`。
6. 重试产生的输入、输出、reasoning token 仍纳入总 token 统计，并增加
   `num_tool_call_retries`。
7. 连续 5 次仍然没有原生调用时抛出 `ToolCallProtocolError` 干净终止，不再把最后一条乱码
   写回历史，避免进入最多 100 轮的无效循环。
8. `reset()` 同步重置 `num_tool_call_retries`，避免复用 agent 时统计字段缺失。

## 五、真实网关复测结果

所有命令执行均经过诊断安全护栏。普通 shell 测试只允许固定命令：

```text
printf '%s\n' CHAIN_SWE_LIVE_7391
```

长 WeasyPrint prompt 测试中，模型提出的仓库命令全部由护栏拦截，没有实际修改 `/testbed`。

### 5.1 当前工具名的短任务端到端测试

使用 `run_shell / file_editor / finish`，连续运行 3 轮：

| 运行 | API 请求数 | 原生调用链 | 结果 | 漂移重试数 |
|---|---:|---|---|---:|
| `shell_03` | 2 | `run_shell -> finish` | 成功 | 0 |
| `shell_04` | 2 | `run_shell -> finish` | 成功 | 0 |
| `shell_05` | 2 | `run_shell -> finish` | 成功 | 0 |

结果：3/3 完成真实工具结果往返。

### 5.2 原长提示词与当前工具名

使用原 WeasyPrint 长提示词和当前 `run_shell / file_editor` schema：

| 运行 | 首次请求原生调用数 | 结果 |
|---|---:|---|
| `full_run_shell_01` | 1 | 首次即返回标准 `run_shell` 调用 |

这说明当前工具改名组合在真实长提示词下能够正常工作，但单个成功样本不能证明网关已经稳定。

### 5.3 故意恢复高风险 `execute_bash` 的压力测试

为主动触发漂移，诊断脚本临时把 shell schema 名改回 `execute_bash`：

| 运行 | 请求序列 | 结果 |
|---|---|---|
| `full_execute_bash_01` | 前 3 次无原生调用，第 4 次返回标准调用 | 重试成功恢复 |
| `full_execute_bash_02` | 连续 5 次无原生调用 | 重试预算耗尽 |

第一轮证明恢复机制能在前几次响应漂移后救回标准调用。第二轮证明旧报告中“重试 5 次成功率
大于 99%”的估计不适用于当前网关状态，客户端重试只能缓解，不能保证成功。

第二轮的前 4 条被拒绝响应已写入 `tool_call_drift.jsonl`，内容分别包含多种畸形协议。
当时现场测试版本在第 5 次耗尽后仍沿用旧行为；根据该结果，最终源码又进一步收紧为：第 5 条
也作为漂移记录并抛出 `ToolCallProtocolError`，不再污染主历史。该最终行为由单元测试验证。

对压力测试的 5 个请求进行比较，`.messages` 内容完全一致，哈希均为：

```text
4a74deef373428cd8e75398dffb1c2d29da86f1d8df941d8e0b6ccca420dd51c
```

这证明前面的乱码响应确实没有被放入下一次请求上下文。

原始请求、响应、执行记录和统计位于：

```text
/lg-home/cog8/lwli6/ChainSweExp/ChainSWE/analysis/spark_drift_recovery_20260909/
```

其中 `shell_01`、`shell_02` 和 `execute_bash_01` 属于发现并修正诊断脚手架版本/动态 schema
问题之前的无效或不完整运行，不应纳入模型效果统计。有效结果为：

```text
shell_03/
shell_04/
shell_05/
full_execute_bash_01/
full_execute_bash_02/
full_run_shell_01/
```

## 六、代码验证

单元测试覆盖：

- 前两次漂移、第三次原生调用时能够恢复；
- 5 次全部漂移时干净失败，并确保 5 条响应全部作为 discarded 记录；
- 非 OpenAI Chat 协议不启用该兼容重试。

验证结果：

```text
Ran 3 tests
OK
```

相关修改通过 Ruff 静态检查和 Python 语法编译检查。

## 七、脚手架版本漂移发现

诊断容器曾同时存在以下版本：

- 新 `agent.py` / `context_manager.py`；
- 旧工具类名 `run_shell_cmd`；
- 提示词及安全护栏期待 `run_shell`。

由于 `Tool.schema()` 是 classmethod，仅修改工具实例的 `name` 也不会改变实际发送的 schema；
诊断脚本已改为修改工具类名，并增加 `--shell-name`、`--prompt` 和 `--max-iterations` 参数，
方便做受控协议压力测试。

诊断容器 `spark-toolcall-debug` 中的 `agent.py`、`context_manager.py`、shell/file 工具源码和
描述文件目前已经同步到本次版本。但正式实验不能依赖诊断容器内手工同步，应重新构建并安装
完整 wheel。

## 八、限制与后续建议

1. 当前恢复机制只防止错误历史锁死，无法修复网关内部 parser。
2. 连续重试会增加 token 和延迟，应通过 `num_tool_call_retries` 监控实际频率。
3. 如果正式实验中重试耗尽率仍高，不应继续增加几十次重试；应优先切换稳定模型/网关或修复
   服务端解析器。
4. 网关需要重点检查：
   - 为什么 content 中会出现被破坏的工具标签；
   - 为什么偶尔 `finish_reason=tool_calls` 但 `message.tool_calls` 为空；
   - 为什么 forced tool choice 会返回 HTTP 200 却不兑现指定工具；
   - parser 失败后为什么不回滚原始 decode 文本或返回明确错误。
5. 正式批量实验前需要：
   - 在具备 `uv`/build backend 的环境重新构建 SWE-Edit wheel；
   - 在容器中确认 `sweedit.__file__` 和工具 schema 名；
   - 运行一轮短任务 smoke test；
   - 保存首个真实 chain 的原始请求、响应、`tool_call_drift.jsonl` 和 token 统计。

## 九、最终判断

当前证据支持以下定性：

> “每轮乱码且没有工具调用”的直接来源是 Spark 模型/网关概率性输出非原生工具协议；
> SWE-Edit 原来的历史追加策略进一步将首轮漂移放大成多轮锁死。工具改名降低了触发概率，
> 新增的 off-history 重试与干净失败机制阻止了自我强化，但网关端协议问题仍需修复。
