---
license: mit
task_categories:
  - text-generation
language:
  - en
tags:
  - code
  - software-engineering
  - bug-fixing
  - agents
pretty_name: ChainSWE
size_categories:
  - n<1K
configs:
  - config_name: default
    data_files:
      - split: test
        path: data.jsonl
---

# ChainSWE

ChainSWE is a benchmark of sequential, dependent bug fixes for evaluating coding agents on continuous software maintenance. It contains 100 time-ordered chains (304 bug-fix tasks) mined from six SWE-bench-family datasets across 54 Python repositories; each row is one chain over a single repository sharing one base commit and pre-built Docker image, and its `bug_fixes` field lists the tasks in chronological order, where each task is a self-contained SWE-bench-style instance.

## Evaluation

Evaluation is run with the [SWE-Edit](https://github.com/microsoft/SWE-Edit).
