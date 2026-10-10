# 0908 XH 正式测试

本目录是参考 `experiments/0907test_xh_on30` 建立的正式测试配置：30 条 chain，模式为 `oracle`、`seq`、`seq_mem`，同时运行 5 条 chain。模型配置使用 `xh-model`，API 变量名从 `../SWE-Edit/openai_chat.env` 读取，不在 JSON 中保存真实密钥。

当前 SWE-Edit wheel 已使用新的模型工具名：`run_shell`、`file_editor`、`finish`。本实验的 `models_xh.json` 走 Chat Completions（`USE_RESPONSES_API=false`），启动脚本使用当前 `SWE-Edit/dist/sweedit-1.0.0-py3-none-any.whl`。

## 启动

```bash
cd /lg-home/cog8/lwli6/ChainSweExp/ChainSWE
tmux new-session -d -s chainswe_0908_xh30_p5 -c /lg-home/cog8/lwli6/ChainSweExp/ChainSWE \
  ./experiments/0908test_xh/run_in_tmux.sh
```

输出会写入本目录的 `output/`：

```text
output/
├── chain_runs/<chain>/chains/<chain>/<mode>/chain_result.json
├── logs/<chain>.log
├── launcher.log
├── launcher_config.json
├── manifest_30.json
├── summarize.log
└── aggregate/
```

## 监控命令

```bash
tmux has-session -t chainswe_0908_xh30_p5 && echo RUNNING || echo STOPPED
tmux attach -t chainswe_0908_xh30_p5
```

单次查看全部 chain：

```bash
python3 /lg-home/cog8/lwli6/ChainSweExp/ChainSWE/scripts/tool/monitor_0908_xh.py \
  --run-dir /lg-home/cog8/lwli6/ChainSweExp/ChainSWE/experiments/0908test_xh/output --all
```

每 15 秒刷新：

```bash
watch -n 15 'python3 /lg-home/cog8/lwli6/ChainSweExp/ChainSWE/scripts/tool/monitor_0908_xh.py --run-dir /lg-home/cog8/lwli6/ChainSweExp/ChainSWE/experiments/0908test_xh/output --all'
```

查看 launcher 日志：

```bash
tail -f /lg-home/cog8/lwli6/ChainSweExp/ChainSWE/experiments/0908test_xh/output/launcher.log
```

实验结束后手动汇总：

```bash
python3 /lg-home/cog8/lwli6/ChainSweExp/ChainSWE/scripts/tool/summarize_0908_xh.py \
  --run-dir /lg-home/cog8/lwli6/ChainSweExp/ChainSWE/experiments/0908test_xh/output
```
