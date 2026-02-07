# llm.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

import requests


def ollama_is_available(base_url: str = "http://localhost:11434", timeout: int = 2) -> bool:
    try:
        r = requests.get(f"{base_url}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _extract_json_object(text: str) -> Optional[str]:
    """
    Best-effort: extract first valid JSON object/array from an LLM response.
    Handles prose + code fences.
    """
    if not text:
        return None

    # strip code fences
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()

    # find earliest '{' or '['
    obj_start = text.find("{")
    arr_start = text.find("[")
    starts = [x for x in (obj_start, arr_start) if x != -1]
    if not starts:
        return None
    start = min(starts)

    candidate = text[start:]

    # try direct parse
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        pass

    # try trimming end
    for i in range(len(candidate), max(len(candidate) - 12000, 0), -1):
        sub = candidate[:i].strip()
        try:
            json.loads(sub)
            return sub
        except Exception:
            continue

    return None


def _safe_json_loads(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        j = _extract_json_object(text)
        if j is None:
            return None
        try:
            return json.loads(j)
        except Exception:
            return None


@dataclass
class LLMConfig:
    mode: str = "none"  # none|ollama
    model: str = "phi3:mini"
    base_url: str = "http://localhost:11434"
    timeout: int = 120


class NoneLLM:
    """
    Graceful fallback LLM:
    - extract_items returns empty
    - summarize returns a short placeholder manager_summary
    """
    def extract_items(self, chunk_text: str, source: str) -> Dict[str, Any]:
        return {"items": []}

    def summarize(self, items: List[dict]) -> Dict[str, Any]:
        return {
            "manager_summary": [
                "LLM is disabled or unavailable.",
                "Report contains all extracted items sorted by deterministic priority policy.",
                "Run with --llm ollama to generate a manager summary (no API keys required).",
            ]
        }


class OllamaLLM:
    """
    Uses Ollama /api/generate (works even when /api/chat is not available).
    """
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def _generate(self, prompt: str) -> str:
        payload = {
            "model": self.cfg.model,
            "prompt": (
                "SYSTEM: You are a precise assistant. Return ONLY valid JSON. Do not add extra keys.\n\n"
                f"USER:\n{prompt}\n"
            ),
            "stream": False,
            "options": {"temperature": 0.1},
        }
        r = requests.post(f"{self.cfg.base_url}/api/generate", json=payload, timeout=self.cfg.timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("response", "")

    def extract_items(self, chunk_text: str, source: str) -> Dict[str, Any]:
        prompt = f"""
Extract operational updates from the text.

Return ONLY JSON in this exact format:
{{
  "items": [
    {{
      "type":"done|blocker|risk|decision|question|info",
      "priority":"P0|P1|P2",
      "channel":"email|slack|standup|doc",
      "text":"...",
      "owner":null,
      "due":null,
      "flags": {{
        "requires_action": true|false,
        "manager_attention": true|false,
        "financial_risk": true|false,
        "blocking": true|false,
        "informational": true|false
      }},
      "source":"{source}"
    }}
  ]
}}

Rules:
- Keep each item short (<= 200 chars).
- priority:
  - P0: money mismatch, incident/outage, compliance breach, "pause now", urgent escalation
  - P1: needs review today, suspicious spikes, access issues impacting investigation
  - P2: informational updates, monitoring, planning
- channel: pick best match (email/slack/standup/doc).
- flags:
  - requires_action: follow-up needed
  - manager_attention: needs escalation/approval/owner assignment
  - financial_risk: money involved (mismatch, payout, chargeback)
  - blocking: blocks progress (e.g., no access / cannot proceed)
  - informational: purely FYI
- If owner is mentioned, set "owner" (name/role). Otherwise null.
- If a due date is mentioned, set "due" as YYYY-MM-DD. Otherwise null.
- ALWAYS set source exactly to "{source}" for every item.
- Do NOT invent facts. If nothing found, return {{ "items": [] }}.

TEXT:
{chunk_text}
""".strip()

        raw = self._generate(prompt)
        parsed = _safe_json_loads(raw)
        if not isinstance(parsed, dict) or "items" not in parsed or not isinstance(parsed["items"], list):
            return {"items": []}

        out_items: List[Dict[str, Any]] = []
        for it in parsed["items"]:
            if not isinstance(it, dict):
                continue
            it["source"] = source  # enforce
            if "type" not in it or "text" not in it:
                continue
            out_items.append(it)

        return {"items": out_items}

    def summarize(self, items: List[dict]) -> Dict[str, Any]:
        """
        Only manager_summary. Priority & grouping are deterministic in main.py.
        """
        items_json = json.dumps(items, ensure_ascii=False, indent=2)

        prompt = f"""
You will receive extracted operational items as a JSON array.
Return ONLY JSON in this exact format:

{{
  "manager_summary": ["bullet 1", "bullet 2", "bullet 3", "bullet 4", "bullet 5"]
}}

Rules:
- 5–8 bullets max, concise and manager-ready.
- Mention the biggest P0/P1 risks and required actions.
- Do NOT hallucinate: only summarize what's in ITEMS.

ITEMS:
{items_json}
""".strip()

        raw = self._generate(prompt)
        parsed = _safe_json_loads(raw)
        if not isinstance(parsed, dict) or "manager_summary" not in parsed or not isinstance(parsed["manager_summary"], list):
            return NoneLLM().summarize(items)

        bullets: List[str] = []
        for x in parsed.get("manager_summary", []):
            if isinstance(x, str) and x.strip():
                bullets.append(x.strip())

        return {"manager_summary": bullets}