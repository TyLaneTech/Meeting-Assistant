"""AI assistant for Q&A and meeting summarization.

Supports Anthropic (Claude) and OpenAI (GPT) as interchangeable providers.
Provider and model are runtime-configurable via reload_client().
"""
import json
import re
from typing import Callable

import log

Callback = Callable[[str], None]

# Tool definition used for Anthropic structured patch output.
# Array-of-sections format so the model can create, rename, or restructure
# sections freely without being confined to a hardcoded set.
_PATCH_TOOL = {
    "name": "update_summary",
    "description": (
        "Update the meeting summary. Only return sections with genuinely new "
        "high-level content — do not update for minor details or topics already "
        "captured. Return an empty sections array if nothing significant changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": "Sections to create or update. Omit sections that need no changes.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Section heading (no ## prefix). May be an existing or new section name.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["append", "replace"],
                            "description": (
                                "'append': add new content to an existing section. "
                                "'replace': rewrite the section entirely, or create a new one."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Markdown content for this section (no ## heading). "
                                "For 'append': only new content not already present. "
                                "For 'replace': the complete consolidated content. "
                                "Nesting and sub-bullets are encouraged for clarity. "
                                "Timestamps: append [M:SS] after a bullet when it anchors a specific "
                                "decision, commitment, or notable moment — e.g. '- Agreed to delay launch [12:04]'. "
                                "Use [M:SS–M:SS] ranges to mark the span of a key topic or discussion block. "
                                "Do NOT timestamp every bullet — only moments worth jumping to."
                            ),
                        },
                    },
                    "required": ["name", "action", "content"],
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    },
}

# Models that support Anthropic extended thinking
_ANTHROPIC_THINKING_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250219",
    "claude-3-7-sonnet-20250219",
}


def _format_meta_block(meta: dict | None) -> str:
    """Build a human-readable metadata block from session metadata."""
    if not meta:
        return ""

    lines = ["Session metadata:"]

    if meta.get("title"):
        lines.append(f"  Title: {meta['title']}")

    if meta.get("is_live"):
        lines.append("  Status: LIVE — recording is in progress, transcript is growing in real time")
    else:
        lines.append("  Status: Completed recording")

    if meta.get("started_at"):
        lines.append(f"  Started: {meta['started_at']}")
    if meta.get("ended_at"):
        lines.append(f"  Ended: {meta['ended_at']}")

    if meta.get("duration"):
        lines.append(f"  Duration: {meta['duration']}")

    if meta.get("segment_count"):
        lines.append(f"  Transcript segments: {meta['segment_count']}")

    if meta.get("speakers"):
        lines.append(f"  Speakers ({len(meta['speakers'])}): {', '.join(meta['speakers'])}")

    source_parts = []
    if meta.get("has_desktop_audio"):
        source_parts.append("desktop/system audio")
    if meta.get("has_mic_audio"):
        source_parts.append("microphone")
    if source_parts:
        lines.append(f"  Audio sources: {', '.join(source_parts)}")

    if meta.get("custom_prompt"):
        lines.append(f"\n  User-provided context: {meta['custom_prompt']}")

    return "\n".join(lines)


class AIAssistant:

    _SYSTEM_QA = (
        "You are an intelligent meeting assistant with full access to the session's "
        "transcript, metadata, and (when available) the current summary. Your job is "
        "to answer the user's questions about the meeting accurately, concisely, and "
        "with helpful context.\n\n"
        "## What you know\n"
        "- The full transcript with speaker labels and timestamps\n"
        "- Who the speakers are and what they discussed\n"
        "- The timeline and flow of the conversation\n"
        "- The current auto-generated summary (if one exists)\n"
        "- Whether the recording is live or completed\n\n"
        "## Transcript format\n"
        "Each line follows: [M:SS] [Speaker Name] spoken text\n"
        "- Timestamps mark when each segment was spoken\n"
        "- Speaker labels may be auto-generated (\"Speaker 1\") or user-assigned names\n"
        "- The transcript is machine-generated from audio, so expect minor transcription "
        "errors, missing punctuation, or misheard words — interpret charitably\n\n"
        "## How to respond\n"
        "- Answer questions directly and concisely using markdown formatting\n"
        "- When quoting or referencing specific moments, include the timestamp as [M:SS] "
        "so the user can jump to that point in the recording\n"
        "- If the user asks about something not yet discussed, say so clearly\n"
        "- You can cross-reference the summary and transcript — e.g. if asked to elaborate "
        "on a summary bullet point, find the relevant transcript section\n"
        "- If the recording is live, keep in mind more content may arrive after your answer\n"
        "- When speakers are identified by name, use their names naturally in your response\n"
        "- For questions about who said what, be precise about speaker attribution\n"
        "- Timestamps: include [M:SS] when citing a specific quote or moment; use [M:SS–M:SS] "
        "to indicate a span (e.g. the stretch of discussion on a topic). Place the timestamp "
        "after the referenced text, not before. Only timestamp moments worth jumping to — "
        "avoid tagging every sentence or obvious context\n"
        "- Always respond in English regardless of any foreign words or phrases in the transcript"
    )

    _SYSTEM_SUMMARY = (
        "You are a meeting summarization assistant. You produce clear, well-structured "
        "summaries from audio transcripts.\n\n"
        "## Important context\n"
        "- The transcript may be partial, incomplete, or still in progress — the recording "
        "could be live and ongoing, or the audio may have been cut off mid-sentence\n"
        "- Work with whatever content is available; never refuse because the transcript "
        "seems short or incomplete\n\n"
        "## Transcript format\n"
        "Each line follows: [M:SS] [Speaker Name] spoken text\n"
        "- Timestamps mark when each segment was spoken\n"
        "- Speaker labels may be auto-generated (\"Speaker 1\") or user-assigned names\n"
        "- The transcript is machine-generated, so expect minor errors — interpret charitably\n\n"
        "## Output format\n"
        "- Choose section headings that fit the content and context — do not use a fixed "
        "structure. Let the transcript and any user instructions guide what sections to create.\n"
        "- Use markdown (## headings, bullets, **bold**, nesting) for a scannable hierarchy\n"
        "- Attribute key points and decisions to speakers by name when identified\n\n"
        "## Timestamps\n"
        "Timestamps let users jump directly to moments in the recording — use them surgically.\n"
        "- Format: `[M:SS]` for a moment, `[M:SS–M:SS]` for a span (e.g. a topic block)\n"
        "- Place AFTER the relevant bullet or phrase, not at the start: "
        "`- Team agreed to cut scope for v1 [8:14]`\n"
        "- Good candidates: decisions and commitments, action items assigned to someone, "
        "notable quotes or turning points, topic transitions, key disagreements resolved\n"
        "- Skip timestamps on: generic observations, filler content, bullets that are already "
        "obvious from context, or anywhere one per section is already enough\n"
        "- Aim for 1–3 timestamps per section — enough to orient, not so many they lose meaning\n\n"
        "## Quality bar\n"
        "- Keep every section as concise as possible — rich but tight\n"
        "- Do not pad with obvious or low-value bullets; every line should earn its place\n"
        "- Prefer nested structure over long flat lists when topics have sub-points\n"
        "- Always write in English regardless of any foreign words or phrases in the transcript"
    )

    def __init__(self, provider: str = "anthropic", model: str = "claude-sonnet-4-6") -> None:
        self.provider = provider
        self.model = model
        self.client = self._make_client(provider)

    def _make_client(self, provider: str):
        """Create the API client.  Returns None gracefully if no key is set."""
        try:
            if provider == "openai":
                from openai import OpenAI
                return OpenAI()
            import anthropic
            return anthropic.Anthropic()
        except Exception as e:
            print(f"[ai] Could not initialise {provider} client: {e}")
            return None

    def reload_client(self, provider: str | None = None, model: str | None = None) -> None:
        """Re-create the client, optionally changing provider and/or model."""
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        self.client = self._make_client(self.provider)

    def ask(
        self,
        transcript: str,
        chat_history: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        meta: dict | None = None,
    ) -> None:
        """Stream an answer to the latest question in chat_history."""
        meta_block = _format_meta_block(meta)
        summary_block = ""
        if meta and meta.get("current_summary"):
            summary_block = (
                f"\n\nCurrent auto-generated summary:\n---\n"
                f"{meta['current_summary']}\n---"
            )

        system = self._SYSTEM_QA + "\n\n"
        if meta_block:
            system += meta_block + "\n\n"
        system += (
            f"Meeting transcript:\n---\n"
            f"{transcript or '(No transcript yet — meeting may just be starting)'}"
            f"\n---"
            f"{summary_block}"
        )
        self._stream(system, chat_history, on_token, on_done)

    def summarize(
        self,
        transcript: str,
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        custom_prompt: str = "",
        meta: dict | None = None,
    ) -> None:
        """Stream a structured meeting summary from a full transcript."""
        if not transcript.strip():
            on_token("*No transcript available yet — start recording first.*")
            if on_done:
                on_done()
            return

        system = self._SYSTEM_SUMMARY
        meta_block = _format_meta_block(meta)
        if meta_block:
            system += f"\n\n{meta_block}"
        if custom_prompt.strip():
            system += f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"

        prompt = (
            "Summarize this transcript. Choose section headings that fit the content "
            "and any instructions above — do not use a fixed structure.\n\n"
            f"Transcript:\n---\n{transcript}\n---"
        )
        self._stream(
            system,
            [{"role": "user", "content": prompt}],
            on_token,
            on_done,
        )

    def patch_summary(
        self,
        existing_summary: str,
        transcript: str,
        custom_prompt: str = "",
        meta: dict | None = None,
        update_context: str = "",
    ) -> str:
        """Incrementally update a summary using the full transcript.

        The model chooses per-section whether to append new bullets or replace
        the whole section (e.g. for consolidation/deduplication). Sections not
        returned are left untouched, so content can never be silently dropped.
        """
        if not transcript.strip() and not update_context.strip():
            return existing_summary

        meta_block = _format_meta_block(meta)
        meta_note = f"\n\n{meta_block}" if meta_block else ""
        custom_note = (
            f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"
            if custom_prompt.strip() else ""
        )
        update_note = (
            f"\n\nAdditional update context:\n{update_context.strip()}"
            if update_context.strip() else ""
        )

        system_prompt = (
            "You update structured meeting summaries incrementally. You receive the "
            "current summary and the full transcript.\n\n"
            "## When to update\n"
            "ONLY update when genuinely new high-level concepts, decisions, or topics "
            "have been discussed. Do not update for minor elaborations, repetition, or "
            "continued discussion of topics already captured. If nothing significant is "
            "new, return an empty sections array.\n\n"
            "## How to update\n"
            "- 'append': add new content to an existing section\n"
            "- 'replace': rewrite a section entirely (consolidation, deduplication, or "
            "restructuring), or create a new section\n"
            "- Section names are free-form — rename, merge, or create sections as the "
            "content warrants. Let the transcript and user instructions guide structure.\n\n"
            "## Quality bar\n"
            "- Keep all sections as concise as possible — rich but tight\n"
            "- Do not arbitrarily append bullets; update existing ones when appropriate\n"
            "- Use markdown hierarchy and nesting to keep things organised\n"
            "- Timestamps: [M:SS] format (e.g. [4:32]) inline for key moments only\n"
            "- Attribute decisions and points to speakers by name when identified\n"
            "- Always write in English regardless of any foreign words or phrases in the transcript"
        )
        user_prompt = (
            f"Update the summary to reflect any significant new content in the transcript."
            f"{meta_note}{custom_note}{update_note}\n\n"
            f"Current summary:\n---\n{existing_summary}\n---\n\n"
            f"Full transcript:\n---\n{transcript}\n---\n\n"
            f"Return a sections array with only the sections that need changes. "
            f"Each entry: name, action ('append'/'replace'), content (markdown, no ## heading). "
            f"Omit unchanged sections. Return empty sections array if nothing is new."
        )

        try:
            raw = self._complete_structured(system_prompt, user_prompt)
        except Exception as e:
            log.warn("summary", f"patch failed ({e}) — keeping existing summary")
            return existing_summary

        section_updates = raw.get("sections", []) if isinstance(raw, dict) else []
        if not section_updates:
            return existing_summary

        sections = self._parse_sections(existing_summary)
        updated: list[str] = []
        for item in section_updates:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "").strip()
            action = item.get("action", "append")
            content = str(item.get("content", "")).strip()
            if not name or not content:
                continue
            if action == "replace":
                sections[name] = content
            else:  # append
                existing = sections.get(name, "").strip()
                sections[name] = (existing + "\n\n" + content).strip() if existing else content
            updated.append(f"{name}({action})")

        if not updated:
            return existing_summary

        log.info("summary", f"Updated: {updated}")
        return self._build_summary(sections)

    def generate_title(self, transcript: str) -> str:
        """Return a 2-3 word title for the meeting, or '' on failure/no content."""
        if not transcript.strip():
            return ""
        snippet = transcript[:600].strip()
        try:
            raw = self._complete(
                (
                    "You generate ultra-short meeting titles. "
                    "Reply with ONLY 2-3 words in title case. "
                    "No punctuation, no explanation, nothing else."
                ),
                f"Transcript excerpt:\n{snippet}\n\nTitle:",
                #max_tokens=16,
            )
            words = raw.split()[:3]
            return " ".join(words)
        except Exception:
            return ""

    # ── Internal ──────────────────────────────────────────────────────────────

    def _stream(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None,
    ) -> None:
        """Stream tokens from the active provider."""
        try:
            if self.client is None:
                on_token(
                    f"\n\n*Error: No {self.provider.title()} API key configured. "
                    f"Add it in Settings.*"
                )
                return
            if self.provider == "openai":
                self._stream_openai(system, messages, on_token)
            else:
                self._stream_anthropic(system, messages, on_token)
        except Exception as e:
            on_token(f"\n\n*Error: {e}*")
        finally:
            if on_done:
                on_done()

    def _stream_anthropic(self, system: str, messages: list[dict], on_token: Callback) -> None:
        import anthropic
        kwargs: dict = {}
        if self.model in _ANTHROPIC_THINKING_MODELS:
            kwargs["thinking"] = {"type": "adaptive"}
        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=messages,
                **kwargs,
            ) as stream:
                for text in stream.text_stream:
                    on_token(text)
        except anthropic.AuthenticationError:
            on_token("\n\n*Error: Invalid Anthropic API key. Check Settings.*")
        except anthropic.RateLimitError:
            on_token("\n\n*Error: Anthropic rate limit reached. Please wait and retry.*")

    def _stream_openai(self, system: str, messages: list[dict], on_token: Callback) -> None:
        import openai
        full_messages = [{"role": "system", "content": system}] + messages
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                #max_tokens=4096,
                messages=full_messages,
                stream=True,
            )
            for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    on_token(content)
        except openai.AuthenticationError:
            on_token("\n\n*Error: Invalid OpenAI API key. Check Settings.*")
        except openai.RateLimitError:
            on_token("\n\n*Error: OpenAI rate limit reached. Please wait and retry.*")

    def _complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        """Non-streaming single completion from the active provider."""
        if self.client is None:
            raise RuntimeError(
                f"No {self.provider.title()} API key configured. Add it in Settings."
            )
        if self.provider == "openai":
            response = self.client.chat.completions.create(
                model=self.model,
                #max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content.strip()
        else:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()

    def _complete_structured(self, system: str, prompt: str) -> dict:
        """Structured completion returning a dict with section arrays.

        Anthropic: uses tool use so the SDK enforces the schema.
        OpenAI: uses json_object response format + prompt instructions.
        Returns {} on empty or unparseable responses.
        """
        if self.client is None:
            raise RuntimeError(
                f"No {self.provider.title()} API key configured. Add it in Settings."
            )
        if self.provider == "openai":
            schema_hint = (
                ' Respond with valid JSON matching: '
                '{"sections": [{"name": str, "action": "append"|"replace", "content": str}]}. '
                'Use an empty sections array if no updates are needed.'
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system + schema_hint},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            text = response.choices[0].message.content.strip()
            return json.loads(text) if text else {}
        else:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[_PATCH_TOOL],
                tool_choice={"type": "tool", "name": _PATCH_TOOL["name"]},
            )
            for block in response.content:
                if block.type == "tool_use":
                    return block.input or {}
            return {}

    # ── Summary helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_sections(summary: str) -> dict[str, str]:
        """Parse a ## markdown summary into {section_name: content} dict."""
        sections: dict[str, str] = {}
        current: str | None = None
        lines: list[str] = []
        for line in summary.splitlines():
            m = re.match(r"^##\s+(.+)$", line)
            if m:
                if current is not None:
                    sections[current] = "\n".join(lines).strip()
                current = m.group(1).strip()
                lines = []
            elif current is not None:
                lines.append(line)
        if current is not None:
            sections[current] = "\n".join(lines).strip()
        return sections

    @staticmethod
    def _build_summary(sections: dict[str, str]) -> str:
        """Rebuild markdown from a sections dict, preserving insertion order."""
        return "\n\n".join(
            f"## {name}\n{content}"
            for name, content in sections.items()
            if content.strip()
        )
