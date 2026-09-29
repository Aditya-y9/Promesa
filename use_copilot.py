#!/usr/bin/env python3
"""
use_copilot.py
=============
Invoke GitHub Copilot programmatically.

This module provides several ways to call GitHub Copilot from scripts, CI/CD
pipelines and automation without opening the editor UI. It is based on the answers
in this Stack Overflow discussion:

    https://stackoverflow.com/questions/76741410/how-to-invoke-github-copilot-programmatically

The approaches, from most to least supported:

1. Copilot CLI (OFFICIAL, recommended)
   `copilot -p "your prompt"` runs a one-shot prompt non-interactively.
   See: https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically

2. GitHub Copilot cloud agent REST API (OFFICIAL, requires Copilot paid seat)
   Start / list agent tasks via the GitHub REST API with a fine-grained PAT.

3. Undocumented Copilot Chat web API (`/chat/completions`)
   What CopilotChat.nvim and copilot-api wrap. Fragile - requires a token
   minted from your OAuth session / cookies and may break without notice.

This file focuses on approach #1 (official) with a clean, scriptable API, and
includes a reference client for approach #3 for those who want raw prompts.

Usage examples
--------------
    # Ask a single question (returns the text answer on stdout)
    python use_copilot.py --prompt "Explain what an LRU cache is"

    # Use as a module
    from use_copilot import copilot_cli
    out = copilot_cli("Refactor this function to be async: " + code)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "CopilotError",
    "CopilotResult",
    "copilot_cli",
    "CopilotChatClient",
]


# --------------------------------------------------------------------------- #
# Approach 1 - Copilot CLI (official)
# --------------------------------------------------------------------------- #

class CopilotError(RuntimeError):
    """Raised when a Copilot invocation fails."""


@dataclass
class CopilotResult:
    """Result of a Copilot CLI invocation."""

    prompt: str
    stdout: str
    stderr: str = ""
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def answer(self) -> str:
        """The model text answer (stdout cleaned up)."""
        return self.stdout.strip()


def _copilot_binary() -> str:
    """Return the path to the copilot CLI, raising if it is not installed."""
    path = shutil.which("copilot")
    if not path:
        raise CopilotError(
            "The 'copilot' CLI was not found on PATH. Install it, then run "
            "`copilot auth` to authenticate: "
            "https://github.com/features/copilot#get-copilot-cli"
        )
    return path


def copilot_cli(
    prompt: str,
    *,
    allow_tools: Optional[list[str]] = None,
    allow_urls: Optional[list[str]] = None,
    model: Optional[str] = None,
    timeout: Optional[int] = 300,
    env: Optional[dict[str, str]] = None,
) -> CopilotResult:
    """
    Run a single non-interactive Copilot CLI prompt.

    This is the OFFICIAL way to use Copilot programmatically. It shells out to
    `copilot -p "<prompt>"` and returns the response.

    Security note: give Copilot only the permissions it needs. Use
    allow_tools/allow_urls to be explicit rather than --allow-all.

    Args:
        prompt:      The prompt to send to Copilot.
        allow_tools: List of tool names Copilot may use, e.g. ["Read", "Run"].
        allow_urls:  List of URLs / domains Copilot may fetch, e.g. ["docs.python.org"].
        model:       Optional model override, e.g. "gpt-4o". Passed as --model.
        timeout:     Seconds to wait before timing out (default 300).
        env:         Extra environment variables (merged over the current env).

    Returns:
        A CopilotResult with the model's answer in `.answer`.
    """
    binary = _copilot_binary()
    cmd = [binary, "-p", prompt]

    if allow_tools:
        # Always keep permissions minimal.
        cmd += ["--allow-tool", ",".join(allow_tools) if len(allow_tools) > 1 else allow_tools[0]]
    if allow_urls:
        cmd += ["--allow-url", ",".join(allow_urls) if len(allow_urls) > 1 else allow_urls[0]]
    if model:
        cmd += ["--model", model]

    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=merged_env,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - platform dependent
        raise CopilotError(f"copilot CLI timed out after {timeout}s") from exc
    except FileNotFoundError as exc:  # pragma: no cover
        raise CopilotError(f"Failed to launch copilot CLI: {exc}") from exc

    return CopilotResult(
        prompt=prompt,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


def copilot_pipe(stream: str, *, timeout: Optional[int] = 300) -> CopilotResult:
    """
    Pipe a prompt to Copilot via stdin (`echo "<prompt>" | copilot`).

    Piped input is ignored if you also pass -p/--prompt, so this is a
    distinct approach useful when the prompt is produced by another command.

    Args:
        stream:  The text to feed to copilot on stdin.
        timeout: Seconds to wait before timing out.
    """
    binary = _copilot_binary()
    try:
        proc = subprocess.run(
            [binary],
            input=stream,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise CopilotError(f"copilot CLI timed out after {timeout}s") from exc

    return CopilotResult(prompt=stream, stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


# --------------------------------------------------------------------------- #
# Approach 3 - Undocumented Copilot Chat web API (reference client)
# --------------------------------------------------------------------------- #
# NOTE: This uses GitHub Copilot's internal, undocumented chat endpoint. It is
# the same API that CopilotChat.nvim and the `copilot-api` community projects
# wrap. It is NOT a public/stable API and may break at any time. Use at your
# own risk, and prefer the official CLI above for anything serious.

try:  # requests is optional - only needed for the chat API client
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


@dataclass
class CopilotChatClient:
    """
    Minimal client for the undocumented Copilot Chat `/chat/completions` API.

    Args:
        token:          A Copilot API token minted from your Copilot session.
                        See http://github.com/features/copilot chat auth flow.
        api_base:       Endpoint base; the community endpoint used by copilot-api.
        model:          Model id, e.g. "gpt-4o" or "copilot-gpt-4o".
        system_prompt:  Optional default system message.
        headers:        Additional headers (e.g. "editor-plugin-version").
    """

    token: str
    api_base: str = "https://api.githubcopilot.com/chat/completions"
    model: str = "gpt-4o"
    system_prompt: str = "You are a helpful AI coding assistant."
    headers: dict[str, str] = field(default_factory=dict)
    _history: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if requests is None:  # pragma: no cover
            raise CopilotError("The 'requests' package is required for CopilotChatClient. Run: pip install requests")

    def _transport_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            # A Copilot-compatible editor-UA is normally required.
            "User-Agent": "GitHubCopilot/1.0",
            **self.headers,
        }

    def reset(self) -> None:
        """Clear conversational history."""
        self._history = []

    def chat(self, user_message: str, *, reset: bool = False) -> str:
        """
        Send a chat prompt and return the assistant's reply (keeps history).

        Args:
            user_message: The user turn.
            reset:        If True, clear history before sending.
        """
        if reset:
            self.reset()

        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(self._history)
        messages.append({"role": "user", "content": user_message})

        payload: dict[str, Any] = {
            "messages": messages,
            "model": self.model,
            "temperature": 0.2,
            "top_p": 1,
            "n": 1,
            "stream": False,
        }

        resp = requests.post(
            self.api_base,
            headers=self._transport_headers(),
            data=json.dumps(payload),
        )
        if resp.status_code != 200:
            raise CopilotError(
                f"Copilot chat API returned HTTP {resp.status_code}: {resp.text[:500]}"
            )

        try:
            answer = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CopilotError(f"Unexpected Copilot chat response shape: {resp.text[:500]}") from exc

        self._history.append({"role": "user", "content": user_message})
        self._history.append({"role": "assistant", "content": answer})
        return answer


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def _main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="use_copilot",
        description="Invoke GitHub Copilot programmatically using the official Copilot CLI.",
    )
    parser.add_argument("-p", "--prompt", help="The prompt to send to Copilot.")
    parser.add_argument(
        "--pipe",
        action="store_true",
        help="Read the prompt from stdin instead of -p/--prompt.",
    )
    parser.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        help="Tool Copilot may use (can be repeated). Ex: --allow-tool Read --allow-tool Run",
    )
    parser.add_argument(
        "--allow-url",
        action="append",
        default=[],
        help="URL/domain Copilot may fetch (can be repeated).",
    )
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds (default 300).")

    args = parser.parse_args(argv)

    if args.pipe:
        prompt = sys.stdin.read()
    else:
        prompt = args.prompt
        if not prompt:
            parser.error("Please provide a prompt with -p/--prompt or use --pipe to read from stdin.")

    result = copilot_cli(
        prompt,
        allow_tools=args.allow_tool,
        allow_urls=args.allow_url,
        model=args.model,
        timeout=args.timeout,
    )

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(_main())
