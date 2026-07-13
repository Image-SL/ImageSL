"""
ImageSL — Claude integration.

Two capabilities, both server-side so the API key never leaves Railway:

  * vision_stain_report(): Claude looks at a thumbnail of the slide and reasons
    about the stain type, the likely target chromogen, and tissue quality. Its
    answer is used to tune / label the deterministic deconvolution pipeline.
  * chat_stream(): the in-app conversational assistant (SSE token streaming).

Both degrade gracefully: if ANTHROPIC_API_KEY is unset, the endpoints return a
clear, friendly "AI features are not configured" message instead of crashing.
"""

from __future__ import annotations

import json
import os
from typing import Iterator, Optional

try:
    import anthropic
    _HAVE_SDK = True
except Exception:  # pragma: no cover
    _HAVE_SDK = False


# Default per the Anthropic model guidance; override with IMAGESL_CLAUDE_MODEL.
DEFAULT_MODEL = os.environ.get("IMAGESL_CLAUDE_MODEL", "claude-opus-4-8")
VISION_MODEL = os.environ.get("IMAGESL_VISION_MODEL", DEFAULT_MODEL)

SYSTEM_PROMPT = (
    "You are the ImageSL Assistant, a knowledgeable, concise guide built into a "
    "premium immunohistochemistry (IHC) image-analysis platform. You help "
    "pathologists and researchers interpret DAB / chromogenic stain "
    "quantification, choose analysis settings (background color, target-stain "
    "intensity), and understand results such as positive-area percentage and "
    "optical density. Explain the color-deconvolution method in plain terms when "
    "asked. Be accurate and cautious: you assist analysis, you do not provide a "
    "clinical diagnosis. If a user asks for a diagnosis or medical decision, "
    "remind them results must be confirmed by a qualified pathologist. Keep "
    "answers focused and free of filler."
)

_VISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "stain_type": {"type": "string", "description": "e.g. 'H-DAB', 'H&E', 'AEC', 'unknown'"},
        "target_chromogen_color": {"type": "string", "description": "plain color of the target stain, e.g. 'brown'"},
        "target_is_darker": {"type": "boolean", "description": "true if target stain is darker than the counterstain"},
        "tissue_quality": {"type": "string", "enum": ["good", "fair", "poor"]},
        "suggested_background_hex": {"type": "string", "description": "a neutral background color like '#f5f3ef'"},
        "summary": {"type": "string", "description": "one or two sentences a pathologist would find useful"},
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
    # Zero-arg client resolves ANTHROPIC_API_KEY (and OAuth profiles) itself.
    return anthropic.Anthropic()


# --------------------------------------------------------------------------- #
# Vision reasoning
# --------------------------------------------------------------------------- #

def vision_stain_report(thumbnail_jpeg_b64: str) -> dict:
    """
    Ask Claude to look at the slide thumbnail and return a structured report
    used to label and tune the analysis. Never raises to the caller.
    """
    if not is_configured():
        return {
            "available": False,
            "summary": "AI vision reasoning is not configured (set ANTHROPIC_API_KEY).",
        }
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
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": thumbnail_jpeg_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "This is a downsampled immunohistochemistry slide. "
                                "Identify the staining and answer the schema. The "
                                "'target' stain is the chromogen of interest (often "
                                "brown DAB), distinct from the blue/purple nuclear "
                                "counterstain."
                            ),
                        },
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
    except Exception as exc:  # pragma: no cover - network / quota
        return {"available": False, "summary": f"Vision analysis unavailable: {exc}"}


# --------------------------------------------------------------------------- #
# Chat (streaming)
# --------------------------------------------------------------------------- #

def chat_stream(messages: list[dict], *, context: Optional[str] = None) -> Iterator[str]:
    """
    Yield assistant text chunks for the given conversation.

    `messages` is a list of {"role": "user"|"assistant", "content": str}.
    `context` (optional) is analysis context injected as a system-role message
    so the assistant can reference the current slide's numbers.
    """
    if not is_configured():
        yield (
            "The ImageSL Assistant is not configured on this deployment yet. "
            "Add an ANTHROPIC_API_KEY environment variable on Railway to enable "
            "the built-in AI chat."
        )
        return

    clean = _sanitize_messages(messages)
    if not clean:
        yield "Ask me anything about your IHC analysis to get started."
        return

    api_messages: list[dict] = list(clean)
    if context:
        # Opus 4.8 accepts a trailing system-role message for mid-conversation
        # context without disturbing the cached prefix. It must follow a user
        # turn, which `clean` guarantees (it always ends on a user message).
        api_messages.append({"role": "system", "content": context})

    try:
        client = _client()
        with client.messages.stream(
            model=DEFAULT_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=api_messages,
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
    except Exception as exc:  # pragma: no cover
        # Fallback: if the mid-conversation system message isn't supported,
        # retry once with the context folded into the system prompt.
        try:
            client = _client()
            sys = SYSTEM_PROMPT + (f"\n\nCurrent analysis context:\n{context}" if context else "")
            with client.messages.stream(
                model=DEFAULT_MODEL,
                max_tokens=2048,
                system=sys,
                messages=clean,
            ) as stream:
                for text in stream.text_stream:
                    if text:
                        yield text
        except Exception as exc2:
            yield f"\n[Assistant error: {exc2}]"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _first_text(msg) -> str:
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    """Keep only valid alternating user/assistant text turns, ending on user."""
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content  # merge same-role runs
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    while out and out[-1]["role"] != "user":
        out.pop()
    return out
