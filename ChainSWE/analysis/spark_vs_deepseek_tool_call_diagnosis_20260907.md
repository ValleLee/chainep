# Spark 与 DeepSeek 工具调用轨迹对比诊断

日期：2026-09-07

## 结论

> **2026-09-07 原始 HTTP 探针更新：** 后续使用最小无敏感 prompt 对 Spark
> 网关进行了直接 HTTP 测试。无论使用最小工具 schema，还是原 SWE-Edit 的三个
> 完整工具 schema，`tool_choice=auto` 和强制指定工具时，网关都返回了标准
> `choices[0].message.tool_calls`。因此，不能再把问题概括成“Spark 网关普遍不支持
> 原生工具调用”。更准确的结论是：原实验那一次调用链没有获得原生
> `tool_calls`；问题可能是模型在该 prompt 下生成不稳定、网关存在间歇性转换问题，
> 或原实验实际请求与当前探针存在尚未识别的差异。要最终二分，需要复现并保存原
> 实验的完整请求与原始响应，或查询网关服务端历史日志。

通过在同一条 ChainSWE 数据、同一套 SWE-Edit baseline agent 和同一个
WeasyPrint Docker 镜像上对比 Spark 与 DeepSeek，可以确认：

**Spark 轨迹中没有工具调用，不是轨迹保存功能失效，也不是
SWE-Edit 完全无法执行或记录工具，而是 Spark 调用链没有向 SWE-Edit
返回其所需的 OpenAI 原生 `tool_calls` 结构。**

Spark 实际把“工具调用”生成在普通 assistant 文本中，例如
`<execute_bash...`。这些文本标签还存在缺少 `>`、参数标签不闭合等格式问题。
SWE-Edit 的 OpenAI Chat Completions 适配代码不会解析这种文本，只读取
`choices[0].message.tool_calls`，因此提取结果始终为空，工具不会执行，轨迹中也就
不会产生工具返回。

从原实验本身看，可以将其归类为 **Spark 模型/网关调用链的工具调用协议兼容
问题**。但仅凭落盘轨迹不能进一步严格区分：

1. Spark 模型本身只生成了文本形式的工具标签；或
2. 上游模型生成了结构化工具调用，但网关没有将其正确转换为 OpenAI
   `message.tool_calls`。

要把责任精确定位到模型端或网关序列化端，还需要抓取网关原始 HTTP 响应，或用
同一请求直接调用模型后端进行对照。

后续最小 HTTP 探针已经证明网关当前可以为该模型返回标准工具调用，所以现有证据
更偏向“prompt/采样相关的模型输出不稳定”或“间歇性网关转换异常”，而不是稳定、
必现的网关协议缺失。

## 原始 HTTP 与 SDK 探针结果

诊断脚本位于：

- `scripts/tool/probe_openai_gateway_tool_calls.py`
- `scripts/tool/parse_openai_chat_response.py`

探针依次发送不带工具、`tool_choice=auto`、强制指定工具三种请求。请求和响应分别
保存，Authorization header 和 API key 不会写盘。

使用最小工具 schema 时，auto 和 forced 请求的原始响应都包含有效的：

```json
{
  "finish_reason": "tool_calls",
  "message": {
    "content": null,
    "tool_calls": [
      {
        "id": "call_...",
        "type": "function",
        "function": {
          "name": "diagnostic_echo",
          "arguments": "{\"text\":\"gateway-probe\"}"
        }
      }
    ]
  }
}
```

随后保留无敏感信息的最小 prompt，但替换成原实验的三个完整 SWE-Edit 工具
schema，再次测试 auto 和 forced。两者也都返回标准原生工具调用：

```json
{
  "finish_reason": "tool_calls",
  "message": {
    "content": null,
    "tool_calls": [
      {
        "id": "call_...",
        "type": "function",
        "function": {
          "name": "execute_bash",
          "arguments": "{\"command\":\"gateway-probe\"}"
        }
      }
    ]
  }
}
```

最后在一次性 WeasyPrint 容器中，使用 SWE-Edit 声明的 `openai==2.7.1` 对 forced
请求的原始 JSON 进行离线解析。解析成功，`message.tool_calls` 保留了同一个
`execute_bash` 调用。这排除了“原始 HTTP 正确、但 OpenAI SDK 将工具字段丢失”
这一假设。

输出目录：

- 最小 schema：`analysis/gateway_probe_spark_20260907/`
- 完整 SWE-Edit 工具 schema：
  `analysis/gateway_probe_spark_sweedit_tools_20260907/`
- SDK 离线解析结果：完整 schema 输出目录的
  `forced/sdk_parse_verified.json`

注意：第一版最小探针的自动 verdict 代码曾在“原始调用有效但本机未安装 SDK”时
错误地显示 `NO_NATIVE_TOOL_CALL`/`FORCED_TOOL_CHOICE_NOT_HONORED`；原始 JSON
本身始终包含有效 `tool_calls`。分类逻辑已经修复，完整 SWE-Edit schema 那组结果
使用的是修复后的版本，应以原始响应和后者为准。

## Exact replay 最终定案

在用户明确授权后，又使用原 `kozea__weasyprint-2058` 完整 prompt、原实验落盘的
三个 SWE-Edit Chat Completions 工具 schema、同一个 Spark 模型和同一个网关进行
了 exact replay，包括 baseline、`tool_choice=auto` 和强制 `execute_bash` 三种
请求。

三种请求都没有返回原生 `message.tool_calls`。其中最关键的是 forced 请求：发出的
原始请求明确包含：

```json
"tool_choice": {
  "type": "function",
  "function": {"name": "execute_bash"}
}
```

但网关的 HTTP 200 原始响应却是：

```json
{
  "finish_reason": "stop",
  "message": {
    "role": "assistant",
    "content": "...<execute_bash<arg_key>command</arg_key><arg_value>cd /testbed && ls -la</execute_bash>"
  }
}
```

响应中没有 `message.tool_calls`，且文本标签本身没有形成可依赖的规范格式。forced
响应 ID 为：

```text
cht000d68b1@dx1a07c643e7a2881503
```

用 `openai==2.7.1` 对这份原始 JSON 离线解析成功，但结果仍然是
`tool_call_count=0`。这证明 SDK 没有丢字段，原始 HTTP 响应里本来就没有该字段。

Exact replay 输出目录：

```text
analysis/gateway_probe_spark_exact_sweedit_20260907/
```

其中关键证据为：

- `forced/request.json`：网关实际收到的完整请求，含强制 `tool_choice`；
- `forced/response.raw.json`：网关返回的原始 HTTP body；
- `forced/response.headers.json`：白名单化后的响应头；
- `forced/sdk_parse_verified.json`：同版本 OpenAI SDK 的离线解析结果；
- `summary.json`：三种 case 的自动判定。

与最小 prompt 的结果结合，可得到更精确的判断：

| Prompt / tools | auto | forced |
|---|---|---|
| 最小 prompt + 最小工具 schema | 原生 `tool_calls` | 原生 `tool_calls` |
| 最小 prompt + 完整 SWE-Edit schema | 原生 `tool_calls` | 原生 `tool_calls` |
| 原 WeasyPrint prompt + 完整 SWE-Edit schema | 文本标签 | 文本标签 |

因此问题是 **prompt 依赖的 Spark 模型/网关工具调用协议故障**。在客户端 API 合约
边界上，网关输出不正确：forced tool choice 没有被兑现，且返回了
`finish_reason=stop` 的普通文本。SWE-Edit 对这份响应不执行工具是正确行为，不属于
SWE-Edit 丢失工具调用。

仍然无法仅靠客户端区分网关内部的两种情况：

1. Spark 模型在该长 prompt 下生成了畸形文本工具标签，网关无法转换；
2. 模型内部产生了可转换调用，但网关转换器未正确转换。

需要网关团队使用上述 response ID 查询服务端日志，查看模型原始 decode 输出、
tool parser 输入/输出、forced tool-choice 约束是否真正下发，才能完成内部归因。

### 2026-09-08 保留容器中的 live 复现

创建并保留了诊断容器 `spark-toolcall-debug`，镜像与 WeasyPrint 实验一致：

```text
clisterqj/swechain:starryzhang__sweb.eval.x86_64.kozea_1776_weasyprint-2058
```

容器使用默认隔离网络，只读挂载诊断脚本和单个 API 配置文件，诊断输出目录可写。
镜像最初确实没有安装 `openai`，因此出现过
`ModuleNotFoundError: No module named 'openai'`；在容器中安装固定版本
`openai==2.7.1` 后，使用同一容器、同一 key、同一模型和同一套完整工具 schema
重新进行 live 对照：

| Case | response ID | `finish_reason` | 原生工具调用数 | 结果 |
|---|---|---|---:|---|
| 原 WeasyPrint prompt，forced `execute_bash` | `cht000d3eb5@dx1a07eaf746b2875503` | `stop` | 0 | 畸形文本标签 |
| 最小 prompt，forced `execute_bash` | `cht000dd3e9@dx1a07eafc98729db503` | `tool_calls` | 1 | 标准原生调用 |

两份响应的 SDK 解析都在容器内正常完成。这再次排除了模型名错误、缺少 SDK、
SDK 丢字段和 SWE-Edit 轨迹写盘等解释，同时证明问题由请求内容触发，而不是该
API/模型完全没有工具调用能力。

这次原 prompt 失败响应的 `matched_stop` 为 `null`。因此先前失败样本中的
`matched_stop=5` 是服务端排查线索，但不是故障出现的必要条件；不能把根因简单
归结为 stop 配置。稳定证据仍然是：forced tool choice 在原 prompt 下没有形成
`message.tool_calls`，而是生成到普通 `message.content`。

本次 live 输出：

- `analysis/gateway_probe_spark_persistent_container_exact_20260908/`
- `analysis/gateway_probe_spark_persistent_container_minimal_20260908/`

容器未销毁，后续可通过 `docker exec -it spark-toolcall-debug bash` 继续检查。

### 2026-09-08 去除模板多余换行后的对照

使用当前 `scripts/chainswe_exp/common.py::render_prompt()`、原 stage 1 的
`problem_statement.txt` 和 `/testbed` 重新渲染 prompt。新 prompt 从历史版本的
106 行、2746 字节变为 77 行、2671 字节。除当前模板的排版调整外，其余实验条件
保持不变：同一容器、key、模型、完整工具 schema、forced `execute_bash`、
`max_tokens=512`。

新请求仍然失败：

```text
response ID:    cht000dff92@dx1a07eb9c92820fc503
finish_reason:  stop
matched_stop:   null
tool calls:     0
```

响应正文仍生成畸形文本协议：

```text
<execute_bash<arg_key>command</arg_key><arg_value>cd /testbed && ls -la</execute_bash>
```

因此可以排除“历史模板中的额外自动换行是本次工具调用失败的必要触发条件”。去掉
这些换行没有使 forced tool choice 转为标准 `message.tool_calls`。本次输出位于：

- `analysis/gateway_probe_spark_persistent_container_unwrapped_20260908/`
- `analysis/gateway_probe_spark_persistent_container_unwrapped_20260908_prompt.txt`

### 2026-09-08 工具名消融实验

为检查 `execute_bash` 名称是否触发故障，先从当前 WeasyPrint prompt 中删除唯一的
显式工具名，使 prompt 不包含 `execute_bash` 或 `run_shell`；然后在同一容器中做
两条 forced 单变量对照：

| Schema/tool_choice 名称 | response ID | `finish_reason` | 原生调用数 | 文本输出 |
|---|---|---|---:|---|
| `execute_bash` | `cht000d07bb@dx1a07ecdb15b2881503` | `stop` | 0 | `<execute_bash<arg_key>...` |
| `run_shell` | `cht000d2b08@dx1a07ece13582875503` | `stop` | 0 | `<run_shell<arg_key>...` |

两条响应的 `matched_stop` 都为 `null`。因此可以排除两种假设：prompt 中显式写出
`execute_bash` 触发了问题，或者 `execute_bash` 这个函数名本身不受支持。模型会
读取 schema 中的新名称并相应生成 `<run_shell...>`，说明工具 schema 已进入模型
上下文并被模型识别；故障发生在“生成文本式工具协议但未形成/未转换成原生
`message.tool_calls`”这一层。

输出位于：

- `analysis/gateway_probe_spark_tool_name_ablation_execute_bash_20260908/`
- `analysis/gateway_probe_spark_tool_name_ablation_run_shell_20260908/`
- `analysis/gateway_probe_spark_tool_name_ablation_20260908_prompt.txt`
- `analysis/gateway_probe_spark_tool_name_ablation_20260908_run_shell_tools.jsonl`

### 2026-09-08 当前代码 `run_shell_cmd` 真实 prompt 复测

用户将 `ExecuteBash.name` 正式改为 `run_shell_cmd`，并将当前
`common.py::render_prompt()` 中的对应文字同步更新。本次从当前模板重新渲染真实
WeasyPrint prompt，并使用三个当前工具名
`run_shell_cmd / str_replace_editor / finish` 测试 auto 和 forced 两种模式。

| 模式 | response ID | `finish_reason` | 原生调用数 | 结果 |
|---|---|---|---:|---|
| forced `run_shell_cmd` | `cht000d68ce@dx1a07ee77e662885503` | `stop` | 0 | 普通文本 `<run_shell_cmd...></tool_call>` |
| auto | `cht000db11e@dx1a07ee84a2d29c4503` | `stop` | 0 | 普通文本 `<run_shell_cmd...></tool_call>` |

两条响应的 `matched_stop` 都为 `null`，SDK 也都解析成功但得到 0 个 tool calls。
因此正式改名没有修复 Spark 在真实 SWE-Edit prompt 下的工具协议故障；模型读取了
新名称，但继续输出文本式调用，网关没有返回标准 `message.tool_calls`。

输出位于：

- `analysis/gateway_probe_spark_real_prompt_run_shell_cmd_20260908/`
- `analysis/gateway_probe_spark_real_prompt_run_shell_cmd_auto_20260908/`
- `analysis/gateway_probe_spark_real_prompt_run_shell_cmd_20260908_prompt.txt`
- `analysis/gateway_probe_spark_real_prompt_run_shell_cmd_20260908_tools.jsonl`

补充检查了常见的 `/v1/chat/completions` 路由。相同 exact forced 请求仍然失败：
response ID 为 `cht000d2c6e@dx1a07c6dd1412881503`，结果同样是
`finish_reason=stop`、`matched_stop=5`、普通 content 中的畸形工具标签、无原生
`tool_calls`。因此可以排除当前 `OPENAI_BASE_URL` 缺少 `/v1` 是根因。

网关 IP 反向解析为 `shlg-x02-cnode66`，与当前客户端主机
`shlg-master-56-137` 不是同一台机器。本机没有对应进程、容器、日志挂载或
Kubernetes context。SSH 因缺少可信 host key 而停止，没有绕过主机身份校验。
服务端进一步排查所需的 ID 和字段清单见：
`analysis/spark_gateway_server_log_handoff_20260907.md`。

## 对照实验

两次运行使用同一条 chain：

- Continuous ID：`Kozea_WeasyPrint_20240613`
- Repository：`Kozea/WeasyPrint`
- Stage 1：`kozea__weasyprint-2058`
- Agent：SWE-Edit `baseline`
- Reasoning effort：`medium`
- Docker image：同一个 WeasyPrint ChainSWE 镜像

区别仅在于模型/API 接口：

| 项目 | Spark | DeepSeek |
|---|---:|---:|
| API 路径 | OpenAI Chat Completions | Anthropic Messages |
| 轨迹轮数/响应组 | 74 rounds | 45 reasoning/call/output 组 |
| `execute_bash` | 0 | 32 |
| `str_replace_editor.view` | 0 | 11 |
| `str_replace_editor.create` | 0 | 1 |
| `str_replace_editor.str_replace` | 0 | 1 |
| 结构化工具调用 | 无 | 45 |
| 对应工具返回 | 无 | 45 |

## Spark 侧证据

Spark 的 `tool_call_stats.json` 为：

```json
{
  "execute_bash": 0,
  "str_replace_editor": {},
  "finish": 0
}
```

其轨迹包含 72 条 assistant 消息和 75 条 user 消息，但没有
`function_call`、`tool_use`、`function_call_output` 或其他工具结果记录。

assistant 消息反复出现类似以下普通文本：

```text
<execute_bash<arg_key>command</arg_key><arg_value>ls -la /testbed/</execute_bash>
```

以及：

```text
<execute_bash><command>find /testbed ...</command></execute_bash>
```

这些内容只是 `message.content`，不是 OpenAI Chat Completions 的
`message.tool_calls`。因此 agent 判断本轮没有工具调用，并反复追加：

```text
If you have completed the task, please use the finish tool. Otherwise, continue with your work.
```

Spark 甚至在普通文本中声称已经修改和验证代码，但由于工具统计为零、没有工具
返回，实际上没有任何命令或编辑被执行。这进一步说明不能把该文本当作真实操作。

相关文件：

- Spark trajectory：
  `experiments/0907test_xh_on30/output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/oracle/.agent_state/kozea__weasyprint-2058_oracle/traj.jsonl`
- Spark tool stats：同目录下 `tool_call_stats.json`
- Spark token stats：同目录下 `token_stats.json`

另一个可观测性异常是 Spark 的 token 统计全部为 0，但 `num_rounds=74`。检查代码
后可确认，这一项不能作为网关异常的证据：`context_manager.py` 的主调用统计当前只
读取 `input_tokens` 和 `output_tokens`，而 Chat Completions 的标准字段是
`prompt_tokens` 和 `completion_tokens`。因此 token 归零属于 SWE-Edit 的 Chat
Completions 统计兼容问题，应与工具调用问题分开处理。

## DeepSeek 侧证据

DeepSeek 对照轨迹中明确存在以下连续记录：

```json
{"role": "function_call", "name": "execute_bash", "arguments": {"command": "cd /testbed && ls -la"}}
{"role": "function_call_output", "output": "...", "tool_use_id": "call_..."}
```

本次诊断运行共记录：

```json
{
  "execute_bash": 32,
  "str_replace_editor": {
    "view": 11,
    "create": 1,
    "str_replace": 1
  },
  "finish": 0
}
```

轨迹中共有 45 个结构化 `function_call` 和 45 个一一对应的
`function_call_output`。这证明以下链路可以正常工作：

1. SWE-Edit 向模型声明工具；
2. API 返回结构化工具调用；
3. SWE-Edit 提取并执行工具；
4. 工具 stdout/stderr 返回给模型；
5. 调用及返回值写入 trajectory；
6. 工具调用计数写入 `tool_call_stats.json`。

相关文件：

- DeepSeek trajectory：
  `experiments/0907test_deepseek_weasyprint/output/chain_runs/Kozea_WeasyPrint_20240613/chains/Kozea_WeasyPrint_20240613/oracle/.agent_state/kozea__weasyprint-2058_oracle/traj.jsonl`
- DeepSeek tool stats：同目录下 `tool_call_stats.json`
- DeepSeek token stats：同目录下 `token_stats.json`

DeepSeek 运行在收集到充分诊断证据后被主动停止，以避免继续消耗 API，因此该输出
标记为 `PARTIAL`，不能用于最终任务解决率比较；但它足以验证工具调用及轨迹落盘
链路。

## SWE-Edit 代码行为

`src/sweedit/core/agent.py` 的 `_extract_tool_calls()` 在 OpenAI Chat
Completions 模式下只遍历：

```python
response.choices[0].message.tool_calls
```

当该字段为空时，agent 不会尝试从 `message.content` 中解析 XML 或自定义标签，
而是把本轮视为“没有工具调用”。所以即使 Spark 文本标签语法完全正确，当前代码
也不会执行它；本次轨迹中的标签本身还存在语法不完整问题。

这与 DeepSeek 的结果一致：Anthropic Messages 返回原生 `tool_use` 后，当前代码可
正常提取、执行和记录工具。

## 发现的独立脚手架问题

对照实验还发现一个与 Spark 零工具调用不同的独立问题：

`src/sweedit/tools/execute_bash.py` 中的 SWEBench bash 工具无条件添加：

```python
conda_prefix = ". /opt/miniconda3/etc/profile.d/conda.sh; conda activate testbed"
```

当前 WeasyPrint 镜像不存在 `/opt/miniconda3`，所以 DeepSeek 最初的 bash 调用虽然
被真实执行和记录，但都以如下错误返回：

```text
/bin/sh: 1: .: cannot open /opt/miniconda3/etc/profile.d/conda.sh: No such file
```

DeepSeek 后续自行创建了临时 conda shim，bash 才恢复正常。这个问题应单独修复为
“仅在 conda 脚本存在时激活”，但它不是 Spark 工具调用数为零的原因：Spark 的
调用在到达 `execute_bash` 之前就因为没有结构化 `tool_calls` 而未被执行。

## 最终判断与建议

最终判断：

- 轨迹记录器和工具执行主链路正常，DeepSeek 对照已证实。
- Spark 原实验零工具调用的直接原因是 SDK 交给 SWE-Edit 的响应中没有原生
  `message.tool_calls`，但最新原始 HTTP 探针证明网关当前能够正确返回该字段。
- 因此可以排除 trajectory 写盘故障和“网关完全不支持 tools”，但还不能在模型
  prompt 依赖、间歇性网关转换、以及原实验请求差异之间做最终归因。
- conda 路径硬编码是另一个真实的代码问题，应独立修复并重打 wheel。

建议下一步：

1. 保存一次 Spark 网关的原始 JSON 响应，重点检查
   `choices[0].message.tool_calls`、`finish_reason` 和 `usage`。
2. 用最小请求直接测试 Spark 网关的 OpenAI tools/function-calling 支持。
3. 如果网关支持强制工具选择，可设置 `tool_choice` 做协议探针。
4. 优先让网关返回标准 `message.tool_calls`；不建议依赖脆弱的文本 XML 解析作为
   正式修复。
5. 修复 `ExecuteBashSWEBench` 的 conda 环境探测并重打 SWE-Edit wheel 后，再做
   Spark/DeepSeek 的正式效果对比。
