# Spark 工具调用故障根因：工具名触发模型内置文本协议

日期：2026-09-08
容器：`spark-toolcall-debug`（`clisterqj/swechain:starryzhang__sweb.eval.x86_64.kozea_1776_weasyprint-2058`）

## 结论

根因**不是** prompt 长度、模板换行、prompt 内的 XML 标记、缺少 `/v1`、stop 配置
（`matched_stop`）或 SDK 丢字段 —— 这些本轮全部被实验排除。

根因是**工具名本身**：当 `tools` 中出现 `execute_bash` / `str_replace_editor`
这两个 OpenHands 风格的名字时，`spark-x2.5-Code-PT-SWE-Pro-o-66` 会退回它 SFT
阶段学到的**文本工具协议**，把调用写进 `message.content`，不走原生 tool_calls
通道。把两个工具改名后，同一个 prompt 立刻恢复原生 `tool_calls`。

## 关键证据

同一 exact prompt（原 106 行 WeasyPrint prompt），仅改变工具名与 tool_choice：

| Cell | 原生 tool_calls |
|---|---|
| exact + auto + `execute_bash`/`str_replace_editor` | 0/3 |
| exact + forced + 原名 | 0/3 |
| exact + auto + 仅 `execute_bash`→`run_shell_cmd` | 1/3 |
| exact + auto + 两个都改名（`run_shell_cmd`/`file_editor`） | 4/5 |
| exact + forced + 两个都改名 | 1/5 |
| exact + auto + 原名 + "禁止文本标签" system prompt | 0/5 |

输出目录：

- `gateway_probe_spark_prompt_ablations_20260908/`（prompt 消融，5 个变体全失败）
- `gateway_probe_spark_toolname_ablation_20260908/`（工具改名首次成功）
- `gateway_probe_spark_toolname_matrix_20260908/`（重复矩阵）
- `gateway_probe_spark_toolname_final_20260908/`（最终确认）

复现脚本（容器内）：`/diagnostics/probe_rename.py`、`/diagnostics/probe_matrix.py`、
`/diagnostics/probe_final.py`。

## 推翻的历史结论

`spark_vs_deepseek_tool_call_diagnosis_20260907.md` 认为是 "prompt 依赖的协议
故障"，并怀疑长 prompt / 模板换行。本轮 prompt 消融显示，即使是 3 句话、无 XML、
无 issue 正文的 concise 变体，只要带原始工具名就同样失败。所以"prompt 依赖"是
表象，真正的自变量是工具名。

## 两个独立的次生问题

1. **forced tool_choice 在网关侧未兑现**。SWE-Edit 源码里其实从不传
   `tool_choice`（`src/sweedit/core/agent.py` 只传 `tools=`），所以线上跑的是
   auto。但探针显示强制指定工具时网关返回 HTTP 200 + `finish_reason=stop` +
   无 `tool_calls`，且成功率**低于** auto（1/5 vs 4/5）。这是网关的合约违规，
   应单独报给网关团队。
2. **网关 tool parser 失败时是破坏性的**。落到 content 的文本不是模型原始输出，
   而是被 parser 改写过的残骸：`<execute_bash` 缺 `>`、`<arg_value>` 不闭合、
   出现 `</tool_call>>`、`</<arg_key>timeout</arg_key>` 这类碎片。说明 parser
   识别了 GLM 风格的 `<tool_call>name<arg_key>..<arg_value>..</tool_call>`，
   消费了开标记后解析失败，却把改写过的文本直接倒进 `content`，没有回滚原文。
   这让下游连兜底解析都做不了。

## 建议

1. 立刻可做：把 SWE-Edit 的工具改名（`src/sweedit/tools/execute_bash.py:83`
   的 `name`、`str_replace_editor.py:55`），重打 wheel。0/N → 4/5。
2. 4/5 不够，仍需网关侧修复；把本文件和
   `spark_gateway_server_log_handoff_20260907.md` 一起交给
   `shlg-x02-cnode66` 管理员，重点查 tool parser 为什么在这两个工具名下劣化，
   以及失败时为什么不回滚原始 decode 文本。
3. 在网关修好前，SWE-Edit 可加一个只在 `tool_calls` 为空时启用的文本兜底解析，
   但由于 parser 破坏性改写，兜底不可靠，只能当降级手段。
4. 另两个已知独立问题不受影响，仍需修：`execute_bash.py` 无条件 source
   `/opt/miniconda3`（本镜像不存在）；`context_manager.py` 读
   `input_tokens`/`output_tokens` 而 Chat Completions 给的是
   `prompt_tokens`/`completion_tokens`（导致 token 统计全 0）。

---

## 2026-09-08 晚间修订：本文结论已被更完整的实验修正

上文"根因是工具名"的判断**不完整**。补充实验后的正确结论：

1. 故障是**概率性**的。同一请求（原名 + auto）连打 10 次，3 次成功。单轮
   成功率约 20–30%，不是必现。这解释了为什么其他机器复现不了 —— 只跑一两次
   会大概率撞上成功。
2. 真正让原实验 74 轮全 0 的是**多轮自我强化锁死**：首轮一旦走文本协议，
   SWE-Edit 把那条文本追加进历史，模型此后 100% 沿用文本格式。实测
   历史含文本格式 → 0/6；历史含正确 native 调用 → 6/6。
3. 工具改名仍然有效，但只是把首轮成功率从 ~23% 提到 ~63%，**不是根治**。
4. 最有效的客户端修复是**首轮重试**：拿不到 native tool_calls 就重试首轮，
   5 次可达 >99%；首轮成功后无需再重试。

完整报告、复现脚本与原始响应见：`/csy-opt/cog8/rzhan/spark-test/`
