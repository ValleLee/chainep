# 0907 XH on 30 chains

这是一套只跑严格 F2P/P2P 通过的 30 条 chain 的实验配置目录。它不是一个可以脱离 ChainSWE/SWE-Edit 根目录单独运行的 Python 包；迁移时需要保持下面的目录关系：

```text
ChainSweExp/
├── ChainSWE/
│   ├── data.jsonl
│   ├── scripts/run_experiment.py
│   └── experiments/0907test_xh_on30/   # 本目录
└── SWE-Edit/
    └── dist/sweedit-1.0.0-py3-none-any.whl
```

最稳妥的迁移方式是复制整个 `ChainSweExp/` 目录，或者在新机器保持 `ChainSWE` 与 `SWE-Edit` 为同级目录。只复制本目录本身是不够的。

运行还需要：Python 3、Docker daemon、`docker` 命令、宿主机的 `uv` 命令，以及下面 30 个 Docker 镜像。ChainSWE runner 本身主要使用 Python 标准库；`uv` 会被复制进每个容器，用来安装并启动 SWE-Edit wheel。launcher 会在镜像不存在时尝试 `docker pull`。

- `clisterqj/swechain:*`：30 个 chain 镜像，具体 tag 由 `data.jsonl` 决定；
- `sweedit-1.0.0-py3-none-any.whl`：需要与当前实验版本一致；
- `data.jsonl`：需要与 0829 实验一致，SHA256 为 `5bdf3717a4f0d5e4f4f47152757e98ce706605427fc3669e18adaa0f02b40fad`。

30 个镜像 tag 已列在 [docker_images_30.txt](docker_images_30.txt)。如果新机器可以访问镜像仓库，可以执行 `while read image; do docker pull "$image"; done < docker_images_30.txt`；否则在旧机器上用 `docker save`，新机器用 `docker load`。

## 1. 配置模型 API

启动脚本需要一份实际的模型配置 `models_xh.json`。`models_xh.example.json` 只是模板：复制它后填写准确的 `model_id`，并让 `env` 中的值指向环境文件里实际使用的变量名。`models_xh.json` 只保存变量名映射，不要在其中写入真实 API key。

如果是 OpenAI-compatible API，保持 `api_type: OPENAI`，并配置 `OPENAI_BASE_URL`。当前代码支持两种 OpenAI 工具协议：默认 `USE_RESPONSES_API=true` 走 Responses API；如果网关只支持 Chat Completions，将 `values.USE_RESPONSES_API` 设为字符串 `"false"`。原生 Anthropic API 则使用 `api_type: ANTHROPIC`、`ANTHROPIC_API_KEY` 和可选的 `ANTHROPIC_BASE_URL`。

当前环境文件位于 `SWE-Edit/openai_chat.env`；从 `ChainSWE` 目录运行命令时使用 `../SWE-Edit/openai_chat.env`。通用环境文件示例：

```text
XH_API_KEY=your-secret
XH_BASE_URL=https://your-provider.example/v1
```

不要把真实 key 写入 git。`models` 配置中的 `env` 值是环境变量名，不是 key 本身。

## 2. 先 dry-run

```bash
cd /lg-home/cog8/lwli6/ChainSweExp/ChainSWE
python3 experiments/0907test_xh_on30/run_0907_on30.py \
  --chain-list experiments/0907test_xh_on30/chain_list_30.txt \
  --model-config experiments/0907test_xh_on30/models_xh.json \
  --model xh-model \
  --env-file ../SWE-Edit/openai_chat.env \
  --uv-bin experiments/0907test_xh_on30/tools/bin/uv \
  --run-name dry_run \
  --out-root /tmp/chainswe_0907 \
  --modes oracle seq seq_mem \
  --parallel-chains 5 \
  --dry-run
```

正式运行前，建议在新机器执行迁移检查：

```bash
python3 experiments/0907test_xh_on30/preflight_0907.py \
  --model-config experiments/0907test_xh_on30/models_xh.json \
  --model xh-model \
  --env-file ../SWE-Edit/openai_chat.env \
  --uv-bin experiments/0907test_xh_on30/tools/bin/uv \
  --check-images
```

不加 `--check-images` 时只检查文件、数据集、模型配置、API 环境变量和 Docker daemon；加上后会逐个检查 30 个镜像。实验所需的 `uv` 已复制到 `experiments/0907test_xh_on30/tools/bin/uv`，命令通过 `--uv-bin` 显式指定它；如果不传，脚本会从 `PATH` 查找 `uv`。

预期是 30 条 chain、270 个 stage、90 个 chain-mode task。`--agent-type baseline`、`--reasoning-effort medium`、三个 timeout 默认值均与 0829 实验一致。

## 3. 正式运行

当前实验通过 `run_in_tmux.sh` 启动，使用 5 条 chain 并发：

```bash
tmux new-session -d -s chainswe_0907_xh30_p5 -c /lg-home/cog8/lwli6/ChainSweExp/ChainSWE \
  ./experiments/0907test_xh_on30/run_in_tmux.sh
```

输出直接写入：

```text
experiments/0907test_xh_on30/output/
├── chain_runs/<chain>/chains/<chain>/<mode>/chain_result.json
├── logs/<chain>.log
├── launcher_config.json
├── manifest_30.json
└── aggregate/
```

中断后使用同一 `--run-name` 加 `--resume` 续跑；不要修改同一 run 的模型配置、模式或数据集。

## 4. 汇总指标

启动脚本会在主实验结束后自动汇总。也可以手动重新汇总：

```bash
python3 experiments/0907test_xh_on30/summarize_0907_metrics.py \
  --run-dir experiments/0907test_xh_on30/output
```

结果在该 run 的 `aggregate/` 下。`REPORT.md` 同时展示 raw actual resolution、论文 prefix-censor 后的 per-bug resolution 和整条 chain 通过率。

## 5. 实时监控

单次查看：

```bash
python3 experiments/0907test_xh_on30/monitor_0907.py \
  --run-dir experiments/0907test_xh_on30/output --all
```

每 15 秒刷新一次，并显示每条 chain 在三个模式下的 stage 进度：

```bash
watch -n 15 'python3 experiments/0907test_xh_on30/monitor_0907.py --run-dir experiments/0907test_xh_on30/output --all'
```

输出中的 `oracle:complete:3/3`、`seq:partial:2/3`、`seq_mem:pending:0/3` 分别表示模式状态和已完成 stage 数。`active=` 显示正在运行的 stage，`last=` 显示最近结束 stage 的序号、instance ID 和 `RESOLVED`/失败类型。
