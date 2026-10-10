# 星火真实 Chat 工具链路验证（2026-09-08）

## 结论

本次不是模拟响应：在 `spark-toolcall-debug` 容器中真实访问星火模型，加载
0908 正式实验同一个 SWE-Edit wheel，完成了 3 个受限的端到端小任务。
共 8 次模型请求全部返回原生工具调用，实际执行了 shell、文件创建、文件替换，
并通过原生 `finish` 正常结束。另外，正式 WeasyPrint prompt 的一次首轮请求也成功。

因此，目前可以确认本地 Chat 适配的基本链路能够工作，不是必现的
“发了 Responses 工具 schema”“读错响应字段”或“完全没有回传工具结果”。
不能据此声称长任务稳定、历史失败已经消失、客户端适配毫无问题，或已找到服务端根因。

正式实验仍保持暂停。本次没有修改 SWE-Edit、网关、模型配置或工具名。

## 运行身份与限制

- 容器：`spark-toolcall-debug`。
- 模型：`spark-x2.5-Code-PT-SWE-Pro-o-66`。
- 目的地：现有实验使用的内网星火服务 `10.101.56.66:4081`。
- API：`OPENAI`，`USE_RESPONSES_API=false`，实际请求为 Chat Completions。
- wheel SHA256：`b4d57a02da26be5edfaf93994ad13175d5f57561ffd71dfb93392473e8f9ade3`。
- 安装后的 `agent.py`、`llm.py`、`context_manager.py`、`tools/base.py`、
  `utils/api_config.py` 均已与 wheel 内对应文件逐字节比较，一致。
- 隔离运行环境：Python 3.12.14；OpenAI SDK 2.7.1；Anthropic SDK 1.4.0；httpx 0.28.1。
  SDK 2.7.1 固定为历史 HTTP 探针所用版本；本次未证明所有依赖版本与正式容器完全相同。
- 使用 `SwebenchAgent` 的实际 LLM、上下文、工具 schema、响应解析和工具实现。
- 保留正式 `max_tokens=32768`；未额外设置 `tool_choice`、temperature、stop 或推理参数。
- 诊断限制：shell 小任务最多 4 轮，文件小任务最多 6 轮，外层预算 180 秒，轮间延迟设为 0。
- 执行前加安全检查，仅允许指定打印命令，或指定一次性文本文件的创建/替换/读取，以及 finish。
  所有实际调用均通过检查并进入原工具实现，无模拟工具结果，无文本调用兜底，无协议重试补丁。
- 正式 prompt 仅调用首轮 LLM 并保存解析结果，没有执行它返回的命令。

## 实测结果

| 用例 | 真实请求数 | 原生工具序列 | 结果 | API/执行循环耗时 |
|---|---:|---|---|---:|
| shell 小任务 1 | 2 | run_shell → finish | 成功 | 14.94 秒 |
| shell 小任务 2 | 2 | run_shell → finish | 成功 | 6.44 秒 |
| 文件编辑小任务 | 4 | file_editor.create → file_editor.str_replace → run_shell → finish | 成功 | 32.22 秒 |
| 正式 WeasyPrint prompt 首轮 | 1 | run_shell | 返回原生调用；未执行命令 | 16.66 秒 |

耗时从 agent 调用前开始计，不含准备 Python/依赖及导入时间。小样本不能用于估计长任务成功率。

### shell 两轮证据

第一轮星火调用：

```text
run_shell(command="printf '%s\\n' CHAIN_SWE_LIVE_7391")
```

原执行器实际输出：

```text
CHAIN_SWE_LIVE_7391

Exit Code: 0
```

第二轮发送的消息角色为 `user → assistant → tool`，工具结果通过
`role=tool`、匹配的 `tool_call_id`、真实输出 `content` 回传。
随后星火返回原生 `finish(message="CHAIN_SWE_LIVE_7391")`，原 finish 工具返回 success。

- [用例 1 结果](../../analysis/spark_live_adapter_audit_20260908/agent_01/result.json)
- [用例 1 第一轮请求](../../analysis/spark_live_adapter_audit_20260908/agent_01/request_00.json)
- [用例 1 第二轮真实请求](../../analysis/spark_live_adapter_audit_20260908/agent_01/request_01.json)
- [用例 1 真实执行记录](../../analysis/spark_live_adapter_audit_20260908/agent_01/executions.json)
- [用例 2 结果](../../analysis/spark_live_adapter_audit_20260908/agent_02/result.json)

用例 1 两个响应 ID：

```text
cht000d79c6@dx1a07fe341b829d9503
cht000de893@dx1a07fe34a352881503
```

用例 2 两个响应 ID：

```text
cht000db7d9@dx1a07fe4a6a129d9503
cht000d75e6@dx1a07fe4b19a29c4503
```

### 文件编辑四轮证据

模型在指定的一次性文件中创建 `value=41\n`，用原编辑工具替换为 `value=42\n`，
再调用原 shell 工具读取文件，最后 finish 返回 `value=42`。
既检查了最终文件内容，也检查了真实 shell 输出和 finish 参数。

- [结果](../../analysis/spark_live_adapter_audit_20260908/file_agent_01/result.json)
- [实际工具执行记录](../../analysis/spark_live_adapter_audit_20260908/file_agent_01/executions.json)
- [最终文件](../../analysis/spark_live_adapter_audit_20260908/file_agent_01/workdir/transport_marker.txt)
- [响应记录与 ID](../../analysis/spark_live_adapter_audit_20260908/file_agent_01/responses.json)

### 正式 prompt 首轮

使用 0908 WeasyPrint seq stage 1 的实际 prompt（包含当前工具名 run_shell），
由正式 wheel 构造 tools 和请求。响应 `finish_reason=tool_calls`，包含原生 run_shell。

- [实际请求](../../analysis/spark_live_adapter_audit_20260908/full_first_01/request_00.json)
- [原始响应](../../analysis/spark_live_adapter_audit_20260908/full_first_01/response_00.raw.json)
- 响应 ID：`cht000d79c1@dx1a07fe341ab29d9503`。

后续同 prompt 的复测被平台安全审核拒绝，理由是任务内容发送到模型目的地尚需明确授权。
已逐行核查该 prompt 只有公开 issue、HTML 示例、/testbed 路径和通用指令，并申请复核，
仍被拒绝。因此没有绕过审核、没有继续发送该 prompt，正式首轮只有上述一个样本。

## 对历史判断的修正

1. **响应扩展字段不能直接认定为根因。** 实际第二轮请求保留了
   `reasoning_content`、`provider_specific_fields`、tool_calls 中的 `index`，
   两个 shell 小任务和文件小任务仍成功。说明它们不是本次样本中的必现障碍，
   但不证明所有复杂历史都兼容，也不建议未经要求就删除推理历史。
2. **当前 wheel 的 Chat usage 映射实际可用。** 两个 shell 小任务记录 2 个 API rounds，
   文件小任务记录 4 个 rounds，token 非零。此前总结中的 token 全 0 应回查
   统计提取、字段层级或旧产物，不能直接写成当前 wheel 缺少 Chat usage 适配。
3. **旧探针存在误分类 bug。** 本次最小 diagnostic_echo 的 auto 响应实际有原生
   tool_calls，但探针分类时引用未初始化的 `available_names`，输出 REQUEST_EXCEPTION。
   forced 也返回原生调用。两个原始响应均正常，不能按分类脚本输出算模型失败。
   本次没有修改该旧脚本，避免把诊断改动混入历史证据。
4. **这些成功不能否定历史原始坏响应。** 之前缺少 tool_calls、content 中出现残缺标签的
   HTTP 响应仍是真实证据。还需真实失败请求的渲染模板、生成/解析前后日志或受控复现，
   才能定位初始错误来自请求适配、模型生成还是服务端协议转换。

- [旧探针 auto 原始正常响应](../../analysis/spark_live_adapter_audit_20260908/direct_minimal/auto/response.raw.json)
- [旧探针 auto 误分类](../../analysis/spark_live_adapter_audit_20260908/direct_minimal/auto/result.json)
- [旧探针 forced 原始响应](../../analysis/spark_live_adapter_audit_20260908/direct_minimal/forced/response.raw.json)

## 文件和下一步

- 新增诊断脚本：[audit_spark_live_chat.py](../../scripts/tool/audit_spark_live_chat.py)。
- 全部新证据：`analysis/spark_live_adapter_audit_20260908/`。
- 请求记录不含 Authorization 或 API key；真实 env 文件仅在容器内读取。
- 调试容器中的 Python、uv、wheel、安装依赖和一次性测试产物保留，正式实验未恢复。

下一步应在取得明确目的地/载荷授权后，重复正式 prompt，并从真实失败轮捕获
完整请求（不要直接用经过归一化的 traj.jsonl 替代 wire history）进行同请求对照。
当前证据不支持再优先改工具名，也不支持直接宣告客户端或网关任一方已被完全排除。
