# Spark 网关服务端日志排查交接单

日期：2026-09-07

## 服务定位

- Gateway IP：`10.101.56.66`
- Reverse DNS：`shlg-x02-cnode66`
- Port：`4081`
- HTTP server header：`nginx/1.27.5`
- Model：`spark-x2.5-Code-PT-SWE-Pro-o-66`
- Client host：`shlg-master-56-137` / `10.101.56.137`
- Client user：`lwli6`，UID `20130`

本机没有该网关进程、Docker 容器、日志挂载或 Kubernetes context。现有 SSH 尝试
因本机没有可信 host key 而停止，没有关闭 host-key verification，也没有尝试猜测
凭据。因此以下内部日志字段需要由 `shlg-x02-cnode66` 管理员查询。

## 失败请求：完整 prompt

### Root Chat Completions forced

- Response ID：`cht000d68b1@dx1a07c643e7a2881503`
- HTTP Date：`Mon, 07 Sep 2026 15:02:25 GMT`
- Endpoint：`POST /chat/completions`
- Request：强制 `tool_choice.function.name=execute_bash`
- HTTP status：`200`
- Response `finish_reason`：`stop`
- Response `provider_specific_fields.matched_stop`：`5`
- Response `message.tool_calls`：不存在
- Response `message.content`：包含畸形文本标签
  `<execute_bash<arg_key>...`

### Root Chat Completions auto

- Response ID：`cht000d682d@dx1a07c64270e2881503`
- Endpoint：`POST /chat/completions`
- Request：`tool_choice=auto`
- Response `finish_reason`：`stop`
- Response `provider_specific_fields.matched_stop`：`5`
- Response `message.tool_calls`：不存在
- Response content：包含畸形文本工具标签

### `/v1` forced 复核

- Response ID：`cht000d2c6e@dx1a07c6dd1412881503`
- Endpoint：`POST /v1/chat/completions`
- Request：强制 `tool_choice.function.name=execute_bash`
- Response `finish_reason`：`stop`
- Response `provider_specific_fields.matched_stop`：`5`
- Response `message.tool_calls`：不存在
- Response content：以 `</tool_call>` 结束，但开头仍是畸形
  `<execute_bash<arg_key>...`

## 成功对照：最小 prompt，相同完整工具 schema

### Auto success

- Response ID：`cht000ded8c@dx1a07c580e9d29db503`
- Request：与失败请求相同的三个完整 SWE-Edit 工具 schema；最小 prompt；
  `tool_choice=auto`
- Response `finish_reason`：`tool_calls`
- Response `provider_specific_fields.matched_stop`：`null`
- Response `message.tool_calls[0].function.name`：`execute_bash`

### Forced success

- Response ID：`cht000d4bfd@dx1a07c58221e2881503`
- Request：与失败请求相同的三个完整 SWE-Edit 工具 schema；最小 prompt；强制
  `execute_bash`
- Response `finish_reason`：`tool_calls`
- Response `provider_specific_fields.matched_stop`：`null`
- Response `message.tool_calls[0].function.name`：`execute_bash`

## 服务端需要核对的字段

请对失败 ID 和成功 ID 做逐层 diff：

1. 网关收到的原始 request JSON，尤其是 `tools`、`tool_choice`、`messages`。
2. 实际渲染后发送给模型的完整 chat template/token 序列。
3. `tool_choice` 是否转换成 constrained decoding、grammar 或额外 system prompt，且是否
   真正下发到推理后端。
4. 推理后端配置的 stop strings、stop token IDs，以及 `matched_stop=5` 对应的具体
   stop 内容。
5. 模型原始 decode 文本，必须查看 stop trimming 和 tool parser 处理之前的版本。
6. 启用的 tool parser 名称、版本及 parse error/debug 日志。
7. tool parser 输入文本和输出结构；失败时为什么没有生成 `message.tool_calls`。
8. OpenAI response serializer 为什么在 forced case 返回
   `finish_reason=stop`，而不是拒绝请求或返回 `finish_reason=tool_calls`。
9. Response ID 中 `dx1a07c...` 部分是否标识特定 worker；失败和成功是否路由到不同
   worker、权重、chat template 或 parser 配置。
10. `metadata.weight_version=default` 对应的精确模型权重和 tokenizer/chat-template
    版本。

## 判定标准

- 若 parser 之前的模型原始文本已经是畸形标签：模型输出或 chat-template/tool-choice
  约束有问题。
- 若 parser 之前是合法工具格式、parser 之后变成普通 content：tool parser 或
  OpenAI serializer 有问题。
- 若 forced `tool_choice` 根本没有下发到后端：网关请求转换有问题。
- 若所有失败都集中在某个 worker：worker 配置漂移。
- 若同一 worker 仅完整 prompt 失败：prompt-dependent 模型/tool-parser 协议不稳。

## 客户端证据路径

- Exact root replay：`analysis/gateway_probe_spark_exact_sweedit_20260907/`
- Exact `/v1` replay：`analysis/gateway_probe_spark_exact_v1_retry_20260907/`
- 最小 prompt + 完整 tools：
  `analysis/gateway_probe_spark_sweedit_tools_20260907/`
- 总体诊断：`analysis/spark_vs_deepseek_tool_call_diagnosis_20260907.md`

这些目录不包含 API key 或 Authorization header。
