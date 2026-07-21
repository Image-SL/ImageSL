"""
ImageSL — Claude integration.

Server-side so the API key never leaves the host.

  * vision_stain_report(): Claude looks at a thumbnail and reports the stain.
  * chat_agentic(): the in-app assistant. It is given TOOLS that let it
    re-run the analysis. When the user asks it to fix or improve the result
    ("count less background", "the target is the blue stain", "be stricter"),
    Claude calls a tool, the server recalculates, and the updated numbers +
    images are returned to the browser.

Degrades gracefully: with no ANTHROPIC_API_KEY the endpoints return a clear
"not configured" message instead of crashing.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

try:
    import anthropic
    _HAVE_SDK = True
except Exception:  # pragma: no cover
    _HAVE_SDK = False


DEFAULT_MODEL = os.environ.get("IMAGESL_CLAUDE_MODEL", "claude-opus-4-8")
VISION_MODEL = os.environ.get("IMAGESL_VISION_MODEL", DEFAULT_MODEL)

SYSTEM_PROMPT = (
    "You are the ImageSL Assistant, built into a web tool that quantifies "
    "immunohistochemistry (IHC) staining by color deconvolution. You help the "
    "user get a better measurement of their slide.\n\n"
    "You can actually change the analysis using your tools:\n"
    "- recalculate_analysis: re-runs the measurement with new settings. Use it "
    "when the user wants a different result — e.g. too much/too little tissue is "
    "being counted (adjust background_threshold), the wrong stain is being "
    "measured (switch target_index), or detection is too strict/loose "
    "(threshold_scale).\n"
    "- set_appearance: changes only the recolored preview image (target/counter "
    "stain darkness, background color) — it does NOT change the measured numbers.\n\n"
    "When the user asks you to improve or fix the result, pick the right tool, "
    "call it, then briefly explain what you changed and report the new numbers. "
    "Be concise and accurate. You assist analysis; you never give a clinical "
    "diagnosis — if asked, say results must be confirmed by a qualified "
    "pathologist."
)

# Tool schemas exposed to Claude. The actual work is done by the executor the
# caller passes in (which has access to the cached slide + engine).
TOOLS = [
    {
        "name": "recalculate_analysis",
        "description": (
            "Re-run the IHC quantification on the current slide with adjusted "
            "settings. Use whenever the user wants the measured result changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "background_threshold": {
                    "type": "number",
                    "description": (
                        "Optical-density cutoff separating tissue from bright "
                        "background. Lower (~0.10) counts more faint tissue; "
                        "higher (~0.30) ignores faint areas. Typical 0.10-0.30."
                    ),
                },
                "target_index": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": (
                        "Which separated stain to measure as positive. "
                        "0 = counterstain (nuclei, usually blue/purple). "
                        "1 = target chromogen (usually brown DAB). Switch this if "
                        "the wrong stain is being measured."
                    ),
                },
                "threshold_scale": {
                    "type": "number",
                    "description": (
                        "Multiplier on the automatic positivity threshold. "
                        ">1 (e.g. 1.2) is stricter = fewer positive pixels; "
                        "<1 (e.g. 0.8) is looser = more positive pixels. Default 1.0."
                    ),
                },
                "stain_strictness": {
                    "type": "string",
                    "enum": ["all", "strong"],
                    "description": (
                        "Whether to count all stains or only the strong/dark ones. "
                        "'all' = standard thresholding. 'strong' = Multi-Otsu isolation "
                        "of only the darkest stains (use when the user asks to ignore "
                        "faint/light stains, or only count the very dark stains)."
                    ),
                },
            },
        },
    },
    {
        "name": "set_appearance",
        "description": (
            "Change how the recolored preview image looks. Does NOT change the "
            "measured numbers. Use for 'make the staining darker/lighter' or "
            "'change the background color'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_gain": {
                    "type": "number",
                    "description": "Target-stain intensity in the preview. >1 darker, <1 lighter. Default 1.0.",
                },
                "counterstain_gain": {
                    "type": "number",
                    "description": "Counterstain intensity in the preview. Default 1.0.",
                },
                "background_hex": {
                    "type": "string",
                    "description": "Hex color to paint the detected background, e.g. '#ffffff'. Omit to keep original.",
                },
            },
        },
    },
]

_VISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "stain_type": {"type": "string"},
        "target_chromogen_color": {"type": "string"},
        "target_is_darker": {"type": "boolean"},
        "tissue_quality": {"type": "string", "enum": ["good", "fair", "poor"]},
        "suggested_background_hex": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": [
        "stain_type",
        "target_chromogen_color",
        "target_is_darker",
        "tissue_quality",
        "suggested_background_hex",
        "summary",
    ],
}


def is_configured() -> bool:
    return _HAVE_SDK and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client() -> "anthropic.Anthropic":
    return anthropic.Anthropic()


# --------------------------------------------------------------------------- #
# Vision reasoning
# --------------------------------------------------------------------------- #

def vision_stain_report(thumbnail_jpeg_b64: str) -> dict:
    if not is_configured():
        return {"available": False, "summary": "AI is not configured (set ANTHROPIC_API_KEY)."}
    try:
        client = _client()
        msg = client.messages.create(
            model=VISION_MODEL,
            max_tokens=1024,
            output_config={"format": {"type": "json_schema", "schema": _VISION_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": thumbnail_jpeg_b64}},
                        {"type": "text", "text": (
                            "This is a downsampled immunohistochemistry slide. Identify the "
                            "staining and answer the schema. The 'target' stain is the chromogen "
                            "of interest (often brown DAB), distinct from the blue/purple nuclear "
                            "counterstain."
                        )},
                    ],
                }
            ],
        )
        if getattr(msg, "stop_reason", None) == "refusal":
            return {"available": True, "summary": "The vision model declined to analyze this image."}
        text = _first_text(msg)
        data = json.loads(text) if text else {}
        data["available"] = True
        return data
    except Exception as exc:  # pragma: no cover
        return {"available": False, "summary": f"Vision analysis unavailable: {exc}"}


# --------------------------------------------------------------------------- #
# Agentic chat with recalculation tools
# --------------------------------------------------------------------------- #

def chat_agentic(
    messages: list[dict],
    *,
    context: Optional[str] = None,
    executor: Callable[[str, dict], str],
    max_rounds: int = 6,
) -> dict:
    """
    Run one assistant turn that may call the recalculation tools.

    `executor(tool_name, tool_input) -> str` performs the actual recalculation
    against the cached slide and returns a short text result for Claude.

    Returns {"reply": str, "used_tools": bool}.
    """
    if not is_configured():
        return {
            "reply": "The assistant is not configured on this deployment. Add an "
                     "ANTHROPIC_API_KEY environment variable to enable the AI chat.",
            "used_tools": False,
        }

    clean = _sanitize_messages(messages)
    if not clean:
        return {"reply": "Ask me to adjust or improve the analysis to get started.", "used_tools": False}

    system = SYSTEM_PROMPT
    if context:
        system += "\n\nCurrent analysis state:\n" + context

    api_messages: list = list(clean)
    used = False
    reply_parts: list[str] = []

    try:
        client = _client()
        for _ in range(max_rounds):
            resp = client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=1500,
                system=system,
                tools=TOOLS,
                messages=api_messages,
            )
            api_messages.append({"role": "assistant", "content": resp.content})

            for block in resp.content:
                if getattr(block, "type", None) == "text" and block.text.strip():
                    reply_parts.append(block.text.strip())

            if getattr(resp, "stop_reason", None) != "tool_use":
                break

            used = True
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    try:
                        out = executor(block.name, dict(block.input or {}))
                    except Exception as exc:  # pragma: no cover
                        out = f"error running tool: {exc}"
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": str(out)}
                    )
            api_messages.append({"role": "user", "content": tool_results})
        else:
            reply_parts.append("(stopped after several adjustment rounds)")
    except Exception as exc:  # pragma: no cover
        return {"reply": f"[assistant error: {exc}]", "used_tools": used}

    return {"reply": "\n\n".join(reply_parts).strip() or "Done.", "used_tools": used}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _first_text(msg) -> str:
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    while out and out[-1]["role"] != "user":
        out.pop()
    return out
