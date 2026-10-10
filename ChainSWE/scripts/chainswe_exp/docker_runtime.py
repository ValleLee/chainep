from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .common import safe_name

AGENT_PREFIX = "CHAINSWE_AGENT_RESULT="


def run_command(
    command: list[str],
    *,
    timeout: float | None = None,
    input_bytes: bytes | None = None,
    check: bool = False,
) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        result = {
            "rc": proc.returncode,
            "timeout": False,
            "seconds": round(time.time() - started, 3),
            "output": proc.stdout.decode("utf-8", errors="replace"),
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        result = {
            "rc": 124,
            "timeout": True,
            "seconds": round(time.time() - started, 3),
            "output": output,
        }
    if check and result["rc"] != 0:
        raise RuntimeError(f"Command failed ({result['rc']}): {' '.join(command)}\n{result['output']}")
    return result


def docker_exec(container: str, shell: str, command: str, timeout: float = 300) -> dict[str, Any]:
    return run_command(["docker", "exec", container, shell, "-lc", command], timeout=timeout)


def docker_exec_input(
    container: str,
    command: list[str],
    value: str,
    timeout: float = 120,
) -> dict[str, Any]:
    return run_command(
        ["docker", "exec", "-i", container, *command],
        timeout=timeout,
        input_bytes=value.encode("utf-8", errors="surrogatepass"),
    )


def docker_copy(source: Path, container: str, destination: str) -> None:
    result = run_command(["docker", "cp", str(source), f"{container}:{destination}"], timeout=180)
    if result["rc"] != 0:
        raise RuntimeError(f"docker cp failed: {result['output']}")


def inspect_image(image: str) -> dict[str, Any]:
    result = run_command(["docker", "image", "inspect", image], timeout=60)
    if result["rc"] != 0:
        raise RuntimeError(f"Image unavailable: {image}\n{result['output']}")
    value = json.loads(result["output"])[0]
    return {
        "id": value.get("Id"),
        "repo_digests": value.get("RepoDigests", []),
        "working_dir": value.get("Config", {}).get("WorkingDir", ""),
    }


class ContainerRuntime:
    def __init__(
        self,
        *,
        image: str,
        chain_id: str,
        mode: str,
        output_dir: Path,
        wheel: Path,
        agent_server: Path,
    ) -> None:
        self.image = image
        self.chain_id = chain_id
        self.mode = mode
        self.output_dir = output_dir.resolve()
        self.wheel = wheel.resolve()
        self.agent_server = agent_server.resolve()
        self.container = f"chainswe-exp-{safe_name(chain_id, 34)}-{safe_name(mode, 9)}-{uuid.uuid4().hex[:8]}"
        self.shell = "/bin/sh"
        self.repo_path: str | None = None
        self.image_info: dict[str, Any] = {}

    def start(self, repo_name: str, base_commit: str) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.image_info = inspect_image(self.image)
        result = run_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.container,
                "--label",
                "chainswe.experiment=true",
                "--label",
                f"chainswe.chain={self.chain_id}",
                "--label",
                f"chainswe.mode={self.mode}",
                "--user",
                "root",
                "--volume",
                f"{self.output_dir}:/chainswe-output",
                "--volume",
                "chainswe-uv-cache:/root/.cache/uv",
                "--entrypoint",
                "/bin/sh",
                self.image,
                "-c",
                "trap : TERM INT; while :; do sleep 3600; done",
            ],
            timeout=120,
        )
        if result["rc"] != 0:
            raise RuntimeError(f"Unable to start {self.container}: {result['output']}")

        detected = run_command(
            [
                "docker",
                "exec",
                self.container,
                "/bin/sh",
                "-lc",
                "if [ -x /bin/bash ]; then echo /bin/bash; else echo /bin/sh; fi",
            ],
            timeout=30,
        )
        if detected["output"].strip() in {"/bin/bash", "/bin/sh"}:
            self.shell = detected["output"].strip()

        candidates = []
        for value in [self.image_info.get("working_dir"), "/testbed", "/workspace", "/app", "/repo"]:
            if value and value not in candidates:
                candidates.append(value)
        found = docker_exec(
            self.container,
            self.shell,
            "find / -maxdepth 4 \\( -type d -o -type f \\) -name .git -print 2>/dev/null | head -100",
            timeout=120,
        )
        for line in found["output"].splitlines():
            parent = str(Path(line.strip()).parent)
            if parent and parent not in candidates:
                candidates.append(parent)

        repos: list[str] = []
        for candidate in candidates:
            probe = docker_exec(
                self.container,
                self.shell,
                f"git -C {shlex_quote(candidate)} rev-parse --show-toplevel",
                timeout=30,
            )
            if probe["rc"] == 0:
                top = probe["output"].strip().splitlines()[-1]
                if top not in repos:
                    repos.append(top)

        containing = []
        for repo in repos:
            probe = docker_exec(
                self.container,
                self.shell,
                f"git -C {shlex_quote(repo)} cat-file -e {shlex_quote(base_commit + '^{commit}')}",
                timeout=30,
            )
            if probe["rc"] == 0:
                containing.append(repo)

        wanted = repo_name.split("/")[-1].lower().replace("-", "").replace("_", "")
        choices = containing or repos
        choices.sort(
            key=lambda path: int(Path(path).name.lower().replace("-", "").replace("_", "") == wanted),
            reverse=True,
        )
        if not choices:
            raise RuntimeError(f"No git repository found in image; candidates={repos}")
        # ChainSWE test_cmds are source-benchmark commands and may explicitly
        # `cd /testbed`. The agent must therefore edit the image's real repo,
        # not a workspace copy, or evaluator tests would grade untouched code.
        self.repo_path = choices[0]
        baked_head = self.head()
        declared_exists = self.repo_path in containing
        reset_target = base_commit if declared_exists else (baked_head or "HEAD")
        initialization_mode = (
            "INIT_EXACT"
            if baked_head == base_commit
            else "INIT_DECLARED_BASE"
            if declared_exists
            else "INIT_RELEASE_IMAGE_FALLBACK"
        )
        reset = self.exec(f"git reset --hard {shlex_quote(reset_target)} && git clean -fd", timeout=120)
        if reset["rc"] != 0:
            raise RuntimeError(f"Base reset failed: {reset['output']}")
        selected_base = self.head() or reset_target
        self.image_info.update(
            {
                "declared_base_commit": base_commit,
                "base_commit": selected_base,
                "initialization_mode": initialization_mode,
                "selected_repo": self.repo_path,
                "repo_candidates": repos,
                "repos_containing_declared_base": containing,
                "baked_head": baked_head,
            }
        )
        self.exec(
            "git config user.email evaluation@chainswe.local && git config user.name 'ChainSWE Evaluation'",
            timeout=30,
            check=True,
        )

        self.exec("mkdir -p /agent /chainswe-output/.agent_state", timeout=30, check=True, cwd=False)
        docker_copy(self.wheel, self.container, f"/agent/{self.wheel.name}")
        docker_copy(self.agent_server, self.container, "/agent/agent_server.py")
        uv_path = shutil.which("uv")
        if not uv_path:
            raise RuntimeError("uv executable not found on host")
        docker_copy(Path(uv_path).resolve(), self.container, "/agent/uv")
        self.exec("chmod +x /agent/uv", timeout=30, check=True, cwd=False)

    def exec(
        self,
        command: str,
        *,
        timeout: float = 300,
        check: bool = False,
        cwd: bool = True,
    ) -> dict[str, Any]:
        if cwd:
            if not self.repo_path:
                raise RuntimeError("Container workspace has not been initialized")
            command = f"cd {shlex_quote(self.repo_path)} && {command}"
        result = docker_exec(self.container, self.shell, command, timeout=timeout)
        if check and result["rc"] != 0:
            raise RuntimeError(f"Container command failed: {command}\n{result['output']}")
        return result

    def apply_patch(self, patch: str, *, threeway: bool = False) -> dict[str, Any]:
        if not patch.strip():
            return {"ok": True, "empty": True, "check_rc": 0, "apply_rc": 0, "output": "", "threeway": threeway}
        check_cmd = ["git", "-C", str(self.repo_path), "apply"]
        if threeway:
            check_cmd.append("--3way")
        check_cmd.extend(["--check", "-"])
        check = docker_exec_input(
            self.container,
            check_cmd,
            patch,
        )
        if check["rc"] != 0:
            return {
                "ok": False,
                "empty": False,
                "check_rc": check["rc"],
                "apply_rc": None,
                "output": check["output"],
                "threeway": threeway,
            }
        apply_cmd = ["git", "-C", str(self.repo_path), "apply"]
        if threeway:
            apply_cmd.append("--3way")
        apply_cmd.append("-")
        apply = docker_exec_input(
            self.container,
            apply_cmd,
            patch,
        )
        return {
            "ok": apply["rc"] == 0,
            "empty": False,
            "check_rc": check["rc"],
            "apply_rc": apply["rc"],
            "output": apply["output"],
            "threeway": threeway,
        }

    def head(self) -> str | None:
        result = self.exec("git rev-parse HEAD", timeout=30)
        return result["output"].strip() if result["rc"] == 0 else None

    def status(self) -> str:
        return self.exec("git status --porcelain=v1", timeout=30)["output"]

    def diff(self, start: str, end: str = "HEAD") -> str:
        result = self.exec(f"git --no-pager diff --no-color {shlex_quote(start)} {shlex_quote(end)}", timeout=120)
        if result["rc"] != 0:
            raise RuntimeError(f"Unable to create diff: {result['output']}")
        return result["output"]

    def commit_all(self, message: str, clean_agent_artifacts: bool = False) -> str:
        if clean_agent_artifacts:
            self.exec("rm -f uv.lock", timeout=30)
        self.exec("git add -A", timeout=60, check=True)
        self.exec(f"git commit --allow-empty -m {shlex_quote(message)}", timeout=120, check=True)
        head = self.head()
        if not head:
            raise RuntimeError("Unable to read commit after commit_all")
        return head

    def cleanup_test_artifacts(self, target_head: str = "HEAD") -> None:
        self.exec(f"git reset --hard {shlex_quote(target_head)} && git clean -fd", timeout=120, check=True)

    def close(self) -> None:
        run_command(["docker", "rm", "-f", self.container], timeout=120)


class AgentProcess:
    def __init__(
        self,
        runtime: ContainerRuntime,
        env_values: dict[str, str],
        diagnostic_log: Path,
    ) -> None:
        self.runtime = runtime
        self.env_values = env_values
        self.diagnostic_log = diagnostic_log
        self.diagnostic_log.parent.mkdir(parents=True, exist_ok=True)
        command = ["docker", "exec", "-i"]
        for name in sorted(env_values):
            command.extend(["-e", name])
        command.extend(
            [
                runtime.container,
                "/agent/uv",
                "run",
                "--python",
                "3.12",
                "--with",
                f"/agent/{runtime.wheel.name}",
                "/agent/agent_server.py",
            ]
        )
        child_env = os.environ.copy()
        child_env.update(env_values)
        child_env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=child_env,
        )
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        with self.diagnostic_log.open("a", encoding="utf-8") as log:
            for line in self.process.stdout:
                if line.startswith(AGENT_PREFIX):
                    try:
                        self.responses.put(json.loads(line[len(AGENT_PREFIX) :]))
                    except json.JSONDecodeError as exc:
                        self.responses.put({"status": "bridge_error", "error": str(exc)})
                else:
                    log.write(line)
                    log.flush()

    def request(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise RuntimeError(f"Agent bridge exited with code {self.process.returncode}")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            return self.responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Agent bridge did not respond within {timeout}s") from exc

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.request({"command": "shutdown"}, timeout=30)
        except Exception:
            self.process.kill()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
