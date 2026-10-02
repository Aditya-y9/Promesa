"""
AirLLM Coding Agent
===================
A coding assistant powered by AirLLM — runs large open-source LLMs on
consumer hardware (even a 4GB GPU) for offline code help.

Why AirLLM?
  - Layer-wise inference: loads one layer at a time, so a 70B-class model
    needs only ~4GB of VRAM (or pure CPU / Apple Silicon via MLX).
  - Lossless: no forced quantization, though `--compression 4bit` gives up
    to ~3x speed with minimal accuracy loss.

Usage
-----
  # One-shot question (CLI)
  python airllm_coding_agent.py --model "Qwen/Qwen3-32B" "Write a fast Python fibonacci"

  # One-shot from a file
  python airllm_coding_agent.py --input code_to_review.py "Review this file for bugs"

  # Interactive chat session (keeps history)
  python airllm_coding_agent.py --model "Qwen/Qwen3-32B" --interactive

  # CPU-only inference
  python airllm_coding_agent.py --device cpu "Explain how Git merge works"

  # Check your GPU / CUDA torch setup
  python airllm_coding_agent.py --check-gpu

Prerequisites
-------------
  pip install airllm
  pip install -U bitsandbytes   # optional, for 4bit/8bit acceleration

GPU-enabled torch (CUDA)
------------------------
AirLLM needs a CUDA build of PyTorch to use your GPU. The plain
`pip install airllm` pulls a CPU-only torch. To get CUDA support:

  pip install torch --index-url https://download.pytorch.org/whl/cu121

Then verify with:

  python airllm_coding_agent.py --check-gpu
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# GPU-enabled torch (CUDA)
# ---------------------------------------------------------------------------
def check_gpu() -> None:
    """Report torch / CUDA availability with fix-it guidance."""
    try:
        import torch
    except ImportError as exc:
        print("[gpu] PyTorch is not installed. Install the CUDA build:\n"
              "      pip install torch --index-url https://download.pytorch.org/whl/cu121")
        print(f"[gpu] (import error: {exc})")
        return

    print(f"[gpu] torch version : {torch.__version__}")
    print(f"[gpu] cuda built in : {torch.version.cuda or 'NO'}")
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        vram = torch.cuda.get_device_properties(0).total_memory / 1e9
        print(f"[gpu] device         : {name}")
        print(f"[gpu] capability     : {cap[0]}.{cap[1]}")
        print(f"[gpu] VRAM           : {vram:.1f} GB  -> GPU inference READY")
    else:
        print("[gpu] cuda available : False")
        print("[gpu] -> CUDA torch is NOT enabled. Reinstall torch with CUDA:\n"
              "      pip install torch --index-url https://download.pytorch.org/whl/cu121\n"
              "      (or a newer cu12x wheel for your driver)")


def resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch
        return "gpu" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


# ---------------------------------------------------------------------------
# The AirLLM coding-agent system prompt.
# Tuned so the model behaves like a concise, senior coding assistant.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a senior software engineer and coding agent. "
    "You help write, review, debug, and explain code. "
    "Follow these rules:\n"
    "- Answer concisely; lead with the key idea, then code or steps.\n"
    "- Always show complete, runnable code snippets in fenced blocks.\n"
    "- State assumptions and any missing requirements explicitly.\n"
    "- Point out edge cases, performance issues, and security concerns.\n"
    "- If the request is ambiguous, ask a short clarifying question.\n"
)

MAX_LENGTH = 1024   # max prompt length (context window)
MAX_NEW_TOKENS = 512  # max tokens in a single reply


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
def load_model(model_name: str, compression: str | None, device: str):
    """Load an AirLLM AutoModel.

    AirLLM's AutoModel auto-detects architecture (Qwen, Llama, Mistral,
    DeepSeek, Phi, Gemma, ChatGLM, Baichuan, ...) and handles layer-wise
    inference automatically, so loading looks just like HuggingFace.
    """
    from airllm import AutoModel  # import lazily so --help works without deps

    kwargs: dict = {
        "model_path": model_name,
    }
    if compression:
        kwargs["compression"] = compression  # '4bit' / '8bit' (optional)

    print(f"[airllm] Loading {model_name} (compression={compression}, device={device}) ...")
    model = AutoModel.from_pretrained(**kwargs)
    print("[airllm] Model ready.")

    # AirLLM exposes model.tokenizer and model.generate (HF-compatible).
    return model


def build_prompt(messages: list[dict[str, str]]) -> str:
    """Turn a chat transcript into a single prompt string.

    AirLLM exposes the underlying tokenizer but not always a chat template,
    so we assemble a simple "<|im_start|>"-style transcript that Qwen and
    most instruct models understand well.
    """
    parts = [f"<|im_start|>system\n{SYSTEM_PROMPT.strip()}<|im_end|>\n"]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        parts.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
    return "".join(parts)


def generate_reply(model, prompt: str, device: str) -> str:
    """Tokenize, run AirLLM generate, and decode the model's reply."""
    tokens = model.tokenizer(
        [prompt],
        return_tensors="pt",
        return_attention_mask=False,
        truncation=True,
        max_length=MAX_LENGTH,
        padding=False,
    )

    input_ids = tokens["input_ids"]
    # Move to GPU only if a GPU is available and requested.
    if device == "gpu" and input_ids.is_cuda is False:
        input_ids = input_ids.cuda()

    generation_output = model.generate(
        input_ids,
        max_new_tokens=MAX_NEW_TOKENS,
        use_cache=True,
        return_dict_in_generate=True,
        temperature=0.7,
        top_p=0.9,
    )

    raw = model.tokenizer.decode(generation_output.sequences[0], skip_special_tokens=False)
    # Strip the prompt and any trailing chat markers to isolate the answer.
    assistant_marker = "<|im_start|>assistant\n"
    if assistant_marker in raw:
        raw = raw.split(assistant_marker, 1)[-1]
    for marker in ("<|im_end|>", "<|im_start|>"):
        raw = raw.split(marker, 1)[0]
    return raw.strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="airllm_coding_agent",
        description="Local coding agent powered by AirLLM (run big LLMs on small GPUs).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="The coding question or task (one-shot mode). Ignored with --interactive.",
    )
    parser.add_argument(
        "-m", "--model",
        default="Qwen/Qwen3-32B",
        help="Model repo id or local path. Use a 70B for maximum capability.",
    )
    parser.add_argument(
        "-c", "--compression",
        default=None,
        choices=["4bit", "8bit"],
        help="Optional quantization for faster inference (needs bitsandbytes).",
    )
    parser.add_argument(
        "-d", "--device",
        default="auto",
        choices=["auto", "cpu", "gpu"],
        help="Where to run inference: auto (pick best), cpu, or gpu.",
    )
    parser.add_argument(
        "-i", "--input",
        default=None,
        help="Path to a source file to include in the prompt (code review / analysis).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Start an interactive chat session that keeps conversation history.",
    )
    parser.add_argument(
        "--system",
        default=SYSTEM_PROMPT,
        help="Override the system prompt.",
    )
    parser.add_argument(
        "--check-gpu",
        action="store_true",
        help="Print GPU / CUDA torch status and exit (diagnostics).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.check_gpu:
        check_gpu()
        return 0

    device = resolve_device(args.device)

    model = load_model(args.model, args.compression, device)

    # Build initial messages; include file content if --input was given.
    messages: list[dict[str, str]] = []
    if args.input:
        src = Path(args.input)
        if not src.exists():
            print(f"[error] input file not found: {src}", file=sys.stderr)
            return 1
        content = src.read_text(encoding="utf-8", errors="replace")
        wrap = (
            f"Here is the content of `{src.name}` to review/analyse:\n"
            f"```\n{content}\n```\n"
        )
        messages.append({"role": "user", "content": wrap})

    try:
        if args.interactive:
            return run_interactive(model, device, messages, args.system)
        question = " ".join(args.prompt).strip() or "Hello"
        messages.append({"role": "user", "content": question})
        prompt = build_prompt(messages)
        print(generate_reply(model, prompt, device))
        return 0
    except KeyboardInterrupt:
        print("\n[bye]", file=sys.stderr)
        return 130


def run_interactive(model, device, initial_messages, system_prompt) -> int:
    """Serve an interactive REPL-style chat."""
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = system_prompt
    messages = list(initial_messages)
    print("=== AirLLM Coding Agent (interactive) ===")
    print("Type your coding question. 'exit', 'quit', or Ctrl+C to leave.\n")

    while True:
        try:
            user = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if user.lower() in {"exit", "quit"}:
            break
        if not user:
            continue

        messages.append({"role": "user", "content": user})
        prompt = build_prompt(messages)
        reply = generate_reply(model, prompt, device)
        print(f"\nAgent > {reply}\n")
        # Keep history (bounded) so follow-up questions have context.
        messages.append({"role": "assistant", "content": reply})
        messages = messages[-20:]  # trim very long sessions

    return 0


if __name__ == "__main__":
    sys.exit(main())
