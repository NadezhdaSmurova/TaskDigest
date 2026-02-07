#!/usr/bin/env python3
# main.py — TaskDigest
# - Standups: split by STANDUP: -> chunk -> LLM -> aggregate ONE item per standup src
# - Slack: split by --- -> chunk -> LLM -> aggregate ONE item per slack src
# - Emails: split by Subject: -> chunk -> LLM -> aggregate ONE item per email src
# - No standups/slack/emails sections; only items in P0/P1/P2
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Any, Dict, Tuple, Optional, DefaultDict
from collections import defaultdict

from jinja2 import Template

from llm import LLMConfig, NoneLLM, OllamaLLM, ollama_is_available

SUPPORTED_EXTS = {".txt", ".md", ".json"}
APP_TITLE = "TaskDigest"

# -------------------------
# Standup parsing
# -------------------------для таскдайдже
STANDUP_PLAIN_HEADER_RE = re.compile(r"^\s*STANDUP:\s*(.+?)\s*$", flags=re.MULTILINE)
STANDUP_PLAIN_SPLIT_RE = re.compile(r"(?=^\s*STANDUP:\s*.+?\s*$)", flags=re.MULTILINE)
DATE_LINE_RE = re.compile(r"^\s*DATE:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*$", flags=re.MULTILINE)
STANDUP_SPLIT_RE_FALLBACK = re.compile(r"^\s*---\s*$", flags=re.MULTILINE)

SECTION_LABELS = {
    "DONE": "DONE:",
    "IN_PROGRESS": "IN_PROGRESS:",
    "BLOCKERS": "BLOCKERS:",
    "RISKS": "RISKS:",
    "QUESTIONS": "QUESTIONS:",
}

# Optional markdown standup fallback
STANDUP_MD_HEADER_RE = re.compile(r"^#\s*Daily Standup\s*[–-]\s*.+$", flags=re.MULTILINE)
STANDUP_MD_TITLE_RE = re.compile(r"^#\s*Daily Standup\s*[–-]\s*(.+?)\s*$", flags=re.MULTILINE)

# -------------------------
# Slack parsing
# -------------------------
SLACK_EVENT_SPLIT_RE = re.compile(r"^\s*---\s*$", flags=re.MULTILINE)
SLACK_ROOT_RE = re.compile(r"^\[(\d{2}:\d{2})\]\s*([^:]+?):\s*(.*)\s*$")

# -------------------------
# Email parsing
# -------------------------
EMAIL_SUBJECT_RE = re.compile(r"^\s*Subject:\s*(.+?)\s*$", flags=re.MULTILINE)
EMAIL_SPLIT_RE = re.compile(r"(?=^\s*Subject:\s*)", flags=re.MULTILINE)
EMAIL_FROM_RE = re.compile(r"^\s*From:\s*(.+?)\s*$", flags=re.MULTILINE)

# Priority policy helpers
URGENT_RE = re.compile(r"\burgent\b|\beod\b|\bdeadline\b|\btoday\b", re.IGNORECASE)


# -------------------------
# Helpers
# -------------------------
def _slug(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip()
    # keep original case for slack name? user wants Nadia exactly; but safe to preserve.
    # For slug parts (team), use lowercase underscores.
    sl = s.lower()
    sl = re.sub(r"[^a-z0-9]+", "_", sl)
    sl = re.sub(r"_+", "_", sl).strip("_")
    return sl or "unknown"


def _name_token(name: str) -> str:
    # keep original-ish but no spaces
    n = (name or "").strip()
    n = re.sub(r"\s+", "_", n)
    n = re.sub(r"[^\w]+", "", n)  # keep letters/digits/underscore
    return n or "Unknown"


def standup_src(date_str: Optional[str], team: str) -> str:
    d = date_str.strip() if isinstance(date_str, str) and date_str.strip() else "no_date"
    return f"standup_{d}:{_slug(team)}"


def slack_src(time_hm: str, name: str) -> str:
    return f"slack_{time_hm}:{_name_token(name)}"


def email_src(subject: str) -> str:
    # keep subject human-readable as requested, but normalize whitespace/newlines
    s = re.sub(r"\s+", " ", (subject or "").strip())
    return f"email_{s}" if s else "email_no_subject"


def _norm_for_dedupe(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s:+#-]", "", s)
    return s


def _join_bullets(bullets: List[str], max_items: int = 5) -> str:
    b = [x.strip() for x in (bullets or []) if x and x.strip()]
    if not b:
        return ""
    if len(b) > max_items:
        b = b[:max_items] + [f"(+{len(bullets)-max_items} more)"]
    return "; ".join(b)


def _bullets_from_lines(lines: List[str]) -> List[str]:
    out: List[str] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("- "):
            out.append(s[2:].strip())
        elif s.startswith("• "):
            out.append(s[2:].strip())
    return [x for x in out if x]


def _keep_text(x: str) -> bool:
    s = (x or "").strip().lower()
    return s not in ("none", "n/a", "na", "-")


# -------------------------
# IO
# -------------------------
def load_inputs(input_dir: Path) -> List[Dict[str, str]]:
    if not input_dir.exists():
        raise FileNotFoundError(f"Input dir not found: {input_dir}")

    docs: List[Dict[str, str]] = []
    for p in sorted(input_dir.rglob("*")):
        if p.is_dir():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue

        if p.suffix.lower() == ".json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "text" in data:
                    text = str(data["text"])
                elif isinstance(data, list):
                    text = "\n".join(str(x) for x in data)
                else:
                    text = json.dumps(data, ensure_ascii=False, indent=2)
            except Exception:
                text = p.read_text(encoding="utf-8", errors="ignore")
        else:
            text = p.read_text(encoding="utf-8", errors="ignore")

        text = text.strip()
        if text:
            docs.append({"name": p.name, "text": text})
    return docs


def chunk_text(text: str, max_chars: int, overlap: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    out: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        part = text[start:end].strip()
        if part:
            out.append(part)
        if end >= n:
            break
        start = max(0, end - overlap)
    return out


def make_chunks(docs: List[Dict[str, str]], max_chars: int, overlap: int) -> List[Dict[str, str]]:
    chunks: List[Dict[str, str]] = []
    for d in docs:
        for i, part in enumerate(chunk_text(d["text"], max_chars=max_chars, overlap=overlap)):
            chunks.append({"doc_name": d["name"], "chunk_id": i, "text": part})
    return chunks


# -------------------------
# Type detectors
# -------------------------
def looks_like_any_standup(text: str) -> bool:
    return bool(text and (STANDUP_PLAIN_HEADER_RE.search(text) or STANDUP_MD_HEADER_RE.search(text)))


def looks_like_slack_log(text: str) -> bool:
    if not text:
        return False
    # at least one root-ish slack line
    for line in text.splitlines():
        if line and (not line.startswith(" ")) and SLACK_ROOT_RE.match(line.strip()):
            return True
    return False


def looks_like_email_log(text: str) -> bool:
    return bool(text and EMAIL_SUBJECT_RE.search(text))


# -------------------------
# Standup parsing + aggregation
# -------------------------
def _parse_sections_plain(block: str) -> Dict[str, List[str]]:
    lines = block.splitlines()
    cur = None
    buf: Dict[str, List[str]] = {k: [] for k in SECTION_LABELS.keys()}

    def is_section_header(line: str) -> Optional[str]:
        s = line.strip().upper()
        for k, header in SECTION_LABELS.items():
            if s == header.upper():
                return k
        return None

    for line in lines:
        sec = is_section_header(line)
        if sec is not None:
            cur = sec
            continue
        if cur is None:
            continue
        buf[cur].append(line)

    out: Dict[str, List[str]] = {}
    for k in SECTION_LABELS.keys():
        out[k] = [x for x in _bullets_from_lines(buf.get(k, [])) if _keep_text(x)]
    return out


def parse_standup_plain_cards(text: str, doc_name: str) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    if not text:
        return cards

    blocks = [b.strip() for b in STANDUP_PLAIN_SPLIT_RE.split(text) if b.strip()]

    cleaned_blocks: List[str] = []
    for b in blocks:
        parts = [p.strip() for p in STANDUP_SPLIT_RE_FALLBACK.split(b) if p.strip()]
        cleaned_blocks.append(parts[0] if parts else b)

    for block in cleaned_blocks:
        m = STANDUP_PLAIN_HEADER_RE.search(block)
        if not m:
            continue

        team = m.group(1).strip()
        date_m = DATE_LINE_RE.search(block)
        date_str = date_m.group(1) if date_m else None
        src = standup_src(date_str, team)

        sections = _parse_sections_plain(block)

        cards.append({
            "kind": "standup",
            "src": src,
            "team": team,
            "date": date_str,
            "sections": sections,
            "source_file": doc_name,
            "raw_text": block.strip(),
        })

    return cards


def parse_standup_markdown_cards(text: str, doc_name: str) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    if not text:
        return cards

    m0 = STANDUP_MD_HEADER_RE.search(text)
    if not m0:
        return cards

    text = text[m0.start():]
    blocks = re.split(r"(?=^#\s*Daily Standup\s*[–-]\s*)", text, flags=re.MULTILINE)

    def section_body(block: str, section_title: str) -> str:
        rx = re.compile(
            rf"^##\s*{re.escape(section_title)}\s*\n(.*?)(?=^##\s*|\Z)",
            flags=re.MULTILINE | re.DOTALL,
        )
        mm = rx.search(block)
        return (mm.group(1) if mm else "").strip()

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        m = STANDUP_MD_TITLE_RE.search(block)
        if not m:
            continue

        team = m.group(1).strip()
        date_str = None
        src = standup_src(date_str, team)

        sections = {
            "DONE": [x for x in _bullets_from_lines(section_body(block, "Done").splitlines()) if _keep_text(x)],
            "IN_PROGRESS": [x for x in _bullets_from_lines(section_body(block, "In Progress").splitlines()) if _keep_text(x)],
            "BLOCKERS": [x for x in _bullets_from_lines(section_body(block, "Blockers").splitlines()) if _keep_text(x)],
            "RISKS": [x for x in _bullets_from_lines(section_body(block, "Risks / Concerns").splitlines()) if _keep_text(x)],
            "QUESTIONS": [x for x in _bullets_from_lines(section_body(block, "Questions").splitlines()) if _keep_text(x)],
        }

        cards.append({
            "kind": "standup",
            "src": src,
            "team": team,
            "date": date_str,
            "sections": sections,
            "source_file": doc_name,
            "raw_text": block.strip(),
        })

    return cards


def parse_any_standup_cards(text: str, doc_name: str) -> List[Dict[str, Any]]:
    if STANDUP_PLAIN_HEADER_RE.search(text or ""):
        return parse_standup_plain_cards(text, doc_name)
    if STANDUP_MD_HEADER_RE.search(text or ""):
        return parse_standup_markdown_cards(text, doc_name)
    return []


def aggregate_standup_to_one_item(card: Dict[str, Any], llm_texts: List[str], max_llm_notes: int = 3) -> Dict[str, Any]:
    team = card.get("team") or "Standup"
    date = card.get("date")
    src = card.get("src") or standup_src(date, team)
    sections: Dict[str, List[str]] = (card.get("sections") or {})

    done = sections.get("DONE", []) or []
    inprog = sections.get("IN_PROGRESS", []) or []
    blockers = sections.get("BLOCKERS", []) or []
    risks = sections.get("RISKS", []) or []
    questions = sections.get("QUESTIONS", []) or []

    seen = set()

    def uniq(lst: List[str]) -> List[str]:
        out = []
        for x in lst:
            k = _norm_for_dedupe(x)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(x)
        return out

    done = uniq(done)
    inprog = uniq(inprog)
    blockers = uniq(blockers)
    risks = uniq(risks)
    questions = uniq(questions)

    llm_notes: List[str] = []
    for t in (llm_texts or []):
        t = (t or "").strip()
        if not t:
            continue
        k = _norm_for_dedupe(t)
        if not k or k in seen:
            continue
        if k.startswith("standup:") or k.startswith("standup "):
            continue
        seen.add(k)
        llm_notes.append(t)
        if len(llm_notes) >= max_llm_notes:
            break

    title = f"STANDUP: {team}" + (f" ({date})" if date else "")
    parts: List[str] = [title]
    if inprog:
        parts.append(f"IN_PROGRESS: {_join_bullets(inprog)}")
    if blockers:
        parts.append(f"BLOCKERS: {_join_bullets(blockers)}")
    if risks:
        parts.append(f"RISKS: {_join_bullets(risks)}")
    if questions:
        parts.append(f"QUESTIONS: {_join_bullets(questions)}")
    if done:
        parts.append(f"DONE: {_join_bullets(done)}")
    if llm_notes:
        parts.append(f"NOTES: {_join_bullets(llm_notes)}")

    text = " | ".join([p for p in parts if p])

    return {
        "type": "standup",
        "text": text,
        "channel": "standup",
        "source": src,
        "owner": None,
        "due": date,
        "flags": {"manager_attention": True},
    }


# -------------------------
# Slack parsing + aggregation
# -------------------------
def parse_slack_cards(text: str, doc_name: str) -> List[Dict[str, Any]]:
    """
    Split slack log into events by '---'.
    Root message is the first non-indented line like:
      [09:12] Nadia: ...
    Replies can be indented with tabs/spaces and '↳', but we keep raw.
    """
    cards: List[Dict[str, Any]] = []
    if not text:
        return cards

    blocks = [b.strip("\n") for b in SLACK_EVENT_SPLIT_RE.split(text) if b.strip()]
    for block in blocks:
        root_time = None
        root_name = None
        root_msg = None

        for line in block.splitlines():
            # root line: no leading whitespace
            if not line.strip():
                continue
            if line.startswith(" ") or line.startswith("\t"):
                continue
            m = SLACK_ROOT_RE.match(line.strip())
            if m:
                root_time = m.group(1)
                root_name = m.group(2).strip()
                root_msg = m.group(3).strip()
                break

        if not root_time or not root_name:
            continue

        src = slack_src(root_time, root_name)

        cards.append({
            "kind": "slack",
            "src": src,              # slack_09:12_Nadia
            "time": root_time,
            "name": root_name,
            "root": root_msg or "",
            "source_file": doc_name,
            "raw_text": block.strip(),
        })

    return cards


def aggregate_slack_to_one_item(card: Dict[str, Any], llm_texts: List[str], max_llm_notes: int = 4) -> Dict[str, Any]:
    src = card.get("src") or "slack_unknown"
    time_hm = card.get("time") or "??:??"
    name = card.get("name") or "Unknown"
    root = (card.get("root") or "").strip()

    seen = set()
    if root:
        seen.add(_norm_for_dedupe(root))

    llm_notes: List[str] = []
    for t in (llm_texts or []):
        t = (t or "").strip()
        if not t:
            continue
        k = _norm_for_dedupe(t)
        if not k or k in seen:
            continue
        # avoid echoing the root header
        if k.startswith("slack") or k.startswith("["):
            continue
        seen.add(k)
        llm_notes.append(t)
        if len(llm_notes) >= max_llm_notes:
            break

    # Required: “slack, дата, первое имя” — у нас есть время + имя.
    title = f"SLACK: [{time_hm}] {name}"
    parts = [title]
    if root:
        parts.append(f"ROOT: {root}")
    if llm_notes:
        parts.append(f"NOTES: {_join_bullets(llm_notes, max_items=max_llm_notes)}")

    text = " | ".join([p for p in parts if p])

    return {
        "type": "slack",
        "text": text,
        "channel": "slack",
        "source": src,     # slack_09:12_Nadia
        "owner": None,
        "due": None,
        "flags": {"manager_attention": True},
    }


# -------------------------
# Email parsing + aggregation
# -------------------------
def parse_email_cards(text: str, doc_name: str) -> List[Dict[str, Any]]:
    """
    Split email log into events by Subject: (lookahead).
    """
    cards: List[Dict[str, Any]] = []
    if not text:
        return cards

    blocks = [b.strip() for b in EMAIL_SPLIT_RE.split(text) if b.strip()]
    for block in blocks:
        sm = EMAIL_SUBJECT_RE.search(block)
        if not sm:
            continue
        subj = sm.group(1).strip()
        src = email_src(subj)

        fm = EMAIL_FROM_RE.search(block)
        from_line = fm.group(1).strip() if fm else None

        # body: remove Subject/From first lines to keep something readable
        body = block
        # remove first "Subject:" line
        body = re.sub(r"^\s*Subject:.*\n?", "", body, count=1, flags=re.MULTILINE)
        # keep From line but we already extract it; remove it from body for compactness
        body = re.sub(r"^\s*From:.*\n?", "", body, count=1, flags=re.MULTILINE)
        body = body.strip()

        cards.append({
            "kind": "email",
            "src": src,                 # email_<Subject...>
            "subject": subj,
            "from": from_line,
            "source_file": doc_name,
            "raw_text": block.strip(),
            "body_text": body,
        })

    return cards


def aggregate_email_to_one_item(card: Dict[str, Any], llm_texts: List[str], max_llm_notes: int = 5) -> Dict[str, Any]:
    src = card.get("src") or "email_no_subject"
    subject = card.get("subject") or ""
    from_line = card.get("from")
    body = (card.get("body_text") or "").strip()

    seen = set()
    if subject:
        seen.add(_norm_for_dedupe(subject))

    llm_notes: List[str] = []
    for t in (llm_texts or []):
        t = (t or "").strip()
        if not t:
            continue
        k = _norm_for_dedupe(t)
        if not k or k in seen:
            continue
        # avoid repeating Subject line
        if k.startswith("subject:"):
            continue
        seen.add(k)
        llm_notes.append(t)
        if len(llm_notes) >= max_llm_notes:
            break

    # Keep a short excerpt if LLM notes are empty (so it still has signal)
    excerpt = ""
    if not llm_notes and body:
        # take first ~240 chars
        excerpt = re.sub(r"\s+", " ", body)[:240].strip()

    title = f"EMAIL: {subject}" if subject else "EMAIL"
    parts = [title]
    if from_line:
        parts.append(f"FROM: {from_line}")
    if llm_notes:
        parts.append(f"NOTES: {_join_bullets(llm_notes, max_items=max_llm_notes)}")
    elif excerpt:
        parts.append(f"EXCERPT: {excerpt}")

    text = " | ".join([p for p in parts if p])

    return {
        "type": "email",
        "text": text,
        "channel": "email",
        "source": src,     # email_URGENT: Settlement mismatch...
        "owner": None,
        "due": None,
        "flags": {"manager_attention": True},
    }


# -------------------------
# Chunk builder for events (standup/slack/email)
# -------------------------
def make_event_chunks(cards: List[Dict[str, Any]], max_chars: int, overlap: int) -> List[Dict[str, str]]:
    """
    For each card, chunks over its raw_text.
    Returns dict with:
      kind, src, chunk_id, text
    """
    out: List[Dict[str, str]] = []
    for c in cards:
        src = c.get("src")
        kind = c.get("kind")
        raw = (c.get("raw_text") or "").strip()
        if not src or not kind or not raw:
            continue
        parts = chunk_text(raw, max_chars=max_chars, overlap=overlap)
        for i, part in enumerate(parts):
            out.append({
                "kind": kind,
                "src": src,
                "chunk_id": i,
                "text": part,
            })
    return out


# -------------------------
# Normalization + priority
# -------------------------
def sanitize_priority(p: Any) -> str:
    if not p:
        return "P2"
    p = str(p).strip().upper()
    if "P0" in p:
        return "P0"
    if "P1" in p:
        return "P1"
    if "P2" in p:
        return "P2"
    return "P2"


def prio_rank(p: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2}.get(p or "P2", 2)


def severity(p: str) -> int:
    return {"P0": 2, "P1": 1, "P2": 0}.get(p or "P2", 0)


def infer_channel_from_name(name_or_source: str) -> str:
    n = (name_or_source or "").lower()
    if n.startswith("standup_") or "standup_" in n:
        return "standup"
    if n.startswith("slack_") or "slack_" in n:
        return "slack"
    if n.startswith("email_") or "email_" in n:
        return "email"
    if "slack" in n:
        return "slack"
    if "email" in n or "inbox" in n or "mail" in n:
        return "email"
    return "doc"


def channel_label(ch: str) -> str:
    return {"email": "Email", "slack": "Slack", "standup": "Standup", "doc": "Doc"}.get((ch or "doc").lower(), "Doc")


def normalize_flags(flags: Any) -> Dict[str, bool]:
    base = {
        "requires_action": False,
        "manager_attention": False,
        "financial_risk": False,
        "blocking": False,
        "informational": False,
    }
    if isinstance(flags, dict):
        for k in list(base.keys()):
            if k in flags:
                base[k] = bool(flags.get(k))
    return base


def infer_flags_fallback(it: Dict[str, Any]) -> Dict[str, bool]:
    txt = (it.get("text") or "").lower()

    financial = any(k in txt for k in ["aed", "usd", "payment", "payout", "settlement", "ledger", "chargeback", "refund", "batch", "merchant"])
    requires_action = any(k in txt for k in ["confirm", "investigate", "review", "pause", "escalate", "fix", "follow up", "need a quick review", "ok to", "who owns"])
    no_access = any(k in txt for k in ["no production access", "no access", "cannot access", "can't access", "unable to access", "no dashboard"])
    blocking = (it.get("type") == "blocker") or no_access or ("blockers:" in txt)
    informational = it.get("type") in ("done", "info") and not requires_action and not blocking
    manager_attention = financial or blocking or requires_action or (it.get("channel") in ("standup", "email", "slack"))

    return {
        "requires_action": bool(requires_action),
        "manager_attention": bool(manager_attention),
        "financial_risk": bool(financial),
        "blocking": bool(blocking),
        "informational": bool(informational),
    }


def compute_policy_priority(it: Dict[str, Any]) -> Tuple[str, str]:
    text = (it.get("text") or "").lower()
    t = (it.get("type") or "").lower()
    flags = it.get("flags", {}) or {}

    financial = bool(flags.get("financial_risk")) or any(k in text for k in ["aed", "usd", "settlement", "ledger", "payout", "payment", "chargeback", "refund", "batch", "merchant"])
    mismatch = any(k in text for k in ["mismatch", "difference", "reconcile", "reconciliation", "ledger totals", "missing-row", "doesn't match", "does not match"])
    payments_core = any(k in text for k in ["settlement", "ledger", "payout", "merchant", "batch"])
    urgent = bool(URGENT_RE.search(text)) or ("before eod" in text) or ("may delay" in text) or ("delay" in text)

    fraudish = any(k in text for k in ["abnormal", "suspicious", "fraud", "abuse", "incentive", "affiliate", "chargeback spike", "chargeback spikes"])
    sla = any(k in text for k in ["latency", "spiking", "sla", "response times"])

    no_access = any(k in text for k in ["no production access", "no access", "cannot access", "can't access", "unable to access", "no dashboard"])
    blocking = bool(flags.get("blocking")) or t == "blocker" or no_access or ("blockers:" in text)

    planning = any(k in text for k in ["ui copy", "final qa", "move release", "release to friday", "planning", "fee breakdown", "proposal"])

    # P0
    if financial and mismatch and payments_core:
        return "P0", "settlement/ledger mismatch (money)"
    if financial and payments_core and urgent and mismatch:
        return "P0", "payments risk with today/EOD impact"

    # P1
    if sla:
        return "P1", "SLA risk (latency spike)"
    if fraudish:
        return "P1", "fraud/abuse risk (affiliate anomaly)"
    if blocking:
        return "P1", "investigation blocked (no access)"

    # P2
    if planning:
        return "P2", "planning/routine"
    if bool(flags.get("informational")) or t in ("done", "info"):
        return "P2", "informational update"

    return "P2", "default"


def sanitize_item(it: Any) -> Dict[str, Any]:
    if not isinstance(it, dict):
        return {}

    out = dict(it)
    out["text"] = (out.get("text") or "").strip()
    if not out["text"]:
        return {}

    out["type"] = (out.get("type") or "info").strip().lower()
    out["priority"] = sanitize_priority(out.get("priority"))
    out["source"] = (out.get("source") or "").strip() or "—"

    ch = (out.get("channel") or "").strip().lower()
    if ch not in ("email", "slack", "standup", "doc"):
        ch = infer_channel_from_name(out.get("source", ""))
    out["channel"] = ch

    out["owner"] = out.get("owner")
    out["due"] = out.get("due")

    flags = normalize_flags(out.get("flags"))
    if not any(flags.values()):
        flags = infer_flags_fallback(out)
    out["flags"] = flags

    policy_p, policy_reason = compute_policy_priority(out)
    llm_p = out.get("priority", "P2")
    final_p = policy_p if severity(policy_p) > severity(llm_p) else llm_p

    out["priority"] = final_p
    out["priority_reason"] = {"llm": llm_p, "policy": policy_p, "policy_reason": policy_reason, "final": final_p}

    # remove flags from report payload
    out.pop("flags", None)

    return out


# -------------------------
# Dedup
# -------------------------
def dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        key = (it.get("priority"), it.get("channel"), (it.get("text") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def dedupe_one_event_per_src(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Hard rule: only ONE item per event src in final items.
    Applies to standup_/slack_/email_
    """
    out: List[Dict[str, Any]] = []
    seen_src = set()
    for it in items:
        src = (it.get("source") or "").strip()
        if src.startswith("standup_") or src.startswith("slack_") or src.startswith("email_"):
            if src in seen_src:
                continue
            seen_src.add(src)
        out.append(it)
    return out


# -------------------------
# Report
# -------------------------
def build_report(items: List[Dict[str, Any]], generated_iso: str) -> Dict[str, Any]:
    clean = [sanitize_item(x) for x in items]
    clean = [x for x in clean if x]

    clean = dedupe_one_event_per_src(clean)
    clean = dedupe_items(clean)

    clean.sort(key=lambda x: (prio_rank(x.get("priority")), channel_label(x.get("channel")), x.get("text", "")))

    groups = {"P0": [], "P1": [], "P2": []}
    for it in clean:
        groups[it.get("priority", "P2")].append(it)

    p0 = len(groups.get("P0", []) or [])
    p1 = len(groups.get("P1", []) or [])
    p2 = len(groups.get("P2", []) or [])
    summary_counts = [
        f"P0 - {p0} tasks",
        f"P1 - {p1} tasks",
        f"P2 - {p2} tasks",
    ]

    return {
        "app": APP_TITLE,
        "generated": generated_iso,
        "manager_summary": summary_counts,
        "groups": groups,
        "followups_all": clean,
    }


# -------------------------
# Rendering
# -------------------------
def render_markdown_compact(report: Dict[str, Any]) -> str:
    generated = report.get("generated", "")

    lines: List[str] = []
    lines.append(f"# {APP_TITLE}")
    lines.append(f"_Generated: {generated}_")
    lines.append("")

    ms = report.get("manager_summary", [])
    if ms:
        lines.append("## Manager Summary (LLM)")
        for b in ms[:8]:
            lines.append(f"- {b}")
        lines.append("")

    def header_for(p: str) -> str:
        if p == "P0":
            return "🔥 HIGH / P0"
        if p == "P1":
            return "🟡 MEDIUM / P1"
        return "🟢 LOW / P2"

    groups = report.get("groups", {}) or {}
    for p in ["P0", "P1", "P2"]:
        lines.append(f"## {header_for(p)}")
        arr = groups.get(p, []) or []
        if not arr:
            lines.append("_None_")
            lines.append("")
            continue
        for it in arr:
            ch = channel_label(it.get("channel"))
            lines.append(f"- **[{ch}]** {it.get('text','')}  _(src: {it.get('source','—')})_")
        lines.append("")

    return "\n".join(lines)


def render_html_compact(report: Dict[str, Any]) -> str:
    tpl = Template(
        """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{ app }}</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif; margin: 40px; }
    h1 { margin: 0 0 6px 0; }
    .muted { color: #666; }
    .card { border: 1px solid #eee; border-radius: 14px; padding: 18px; margin: 14px 0; }
    ul { margin: 10px 0 0 18px; }
    li { margin: 10px 0; line-height: 1.35; }
    .src { color: #777; font-size: 12px; margin-left: 8px; white-space: nowrap; }
    .cols { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
    @media (max-width: 980px) { .cols { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <h1>{{ app }}</h1>
  <div class="muted">Generated: {{ generated }}</div>

  {% if manager_summary|length > 0 %}
  <div class="card">
    <h2 style="margin-top:0;">Manager Summary (LLM)</h2>
    <ul>
      {% for b in manager_summary %}
        <li>{{ b }}</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  <div class="cols">
    {% for p in ["P0","P1","P2"] %}
    <div class="card" style="margin:0;">
      <h3 style="margin-top:0;">
        {% if p == "P0" %}🔥 HIGH / P0{% elif p == "P1" %}🟡 MEDIUM / P1{% else %}🟢 LOW / P2{% endif %}
      </h3>
      <ul>
        {% set arr = groups.get(p, []) %}
        {% if arr|length == 0 %}
          <li class="muted">None</li>
        {% endif %}
        {% for it in arr %}
          <li>
            <b>[{{ it.channel_label }}]</b> {{ it.text }}
            <span class="src">src: {{ it.source }}</span>
          </li>
        {% endfor %}
      </ul>
    </div>
    {% endfor %}
  </div>

</body>
</html>
        """
    )

    def pack_item(it: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "priority": it.get("priority", "P2"),
            "channel_label": channel_label(it.get("channel")),
            "text": it.get("text", ""),
            "source": it.get("source", "—"),
        }

    payload = {
        "app": report.get("app", APP_TITLE),
        "generated": report.get("generated", ""),
        "manager_summary": (report.get("manager_summary") or [])[:8],
        "groups": {k: [pack_item(x) for x in v] for k, v in (report.get("groups") or {}).items()},
    }
    return tpl.render(**payload)


# -------------------------
# LLM selection
# -------------------------
def pick_llm(args, extract_timeout: int):
    if args.llm == "ollama":
        if ollama_is_available(base_url=args.ollama_url):
            if extract_timeout != args.timeout:
                extract_llm = OllamaLLM(LLMConfig(mode="ollama", model=args.ollama_model, base_url=args.ollama_url, timeout=extract_timeout))
                summarize_llm = OllamaLLM(LLMConfig(mode="ollama", model=args.ollama_model, base_url=args.ollama_url, timeout=args.timeout))
                return extract_llm, summarize_llm
            llm = OllamaLLM(LLMConfig(mode="ollama", model=args.ollama_model, base_url=args.ollama_url, timeout=args.timeout))
            return llm, llm
        print("[warn] Ollama not available. Falling back to --llm none.")
    none = NoneLLM()
    return none, none


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser(f"{APP_TITLE} (Workflow Automation)")
    ap.add_argument("--input", default="inputs_demo", help="Input folder (txt/md/json)")
    ap.add_argument("--output", default="outputs", help="Output folder")
    ap.add_argument("--llm", default="none", choices=["none", "ollama"], help="LLM mode")
    ap.add_argument("--ollama-model", default="phi3:mini", help="Ollama model name")
    ap.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base url")
    ap.add_argument("--timeout", type=int, default=120, help="LLM summarize timeout seconds")
    ap.add_argument("--extract-timeout", type=int, default=45, help="LLM extract timeout seconds")
    ap.add_argument("--skip-llm-extract", action="store_true", help="Skip LLM extraction step")
    ap.add_argument("--max-chars", type=int, default=1200, help="Chunk size (chars)")
    ap.add_argument("--overlap", type=int, default=120, help="Chunk overlap (chars)")
    ap.add_argument("--standup-llm-notes", type=int, default=3, help="Max extra LLM notes per standup")
    ap.add_argument("--slack-llm-notes", type=int, default=4, help="Max extra LLM notes per slack event")
    ap.add_argument("--email-llm-notes", type=int, default=5, help="Max extra LLM notes per email event")
    args = ap.parse_args()

    in_dir = Path(args.input)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = load_inputs(in_dir)

    # Split docs by type
    standup_docs: List[Dict[str, str]] = []
    slack_docs: List[Dict[str, str]] = []
    email_docs: List[Dict[str, str]] = []
    other_docs: List[Dict[str, str]] = []

    for d in docs:
        t = d["text"]
        if looks_like_any_standup(t):
            standup_docs.append(d)
        elif looks_like_email_log(t):
            email_docs.append(d)
        elif looks_like_slack_log(t):
            slack_docs.append(d)
        else:
            other_docs.append(d)

    # Non-event chunks (normal)
    chunks = make_chunks(other_docs, max_chars=args.max_chars, overlap=args.overlap)

    llm_extract, llm_summarize = pick_llm(args, extract_timeout=args.extract_timeout)

    # Parse event cards
    standup_cards: List[Dict[str, Any]] = []
    for d in standup_docs:
        standup_cards.extend(parse_any_standup_cards(d["text"], doc_name=d["name"]))

    slack_cards: List[Dict[str, Any]] = []
    for d in slack_docs:
        slack_cards.extend(parse_slack_cards(d["text"], doc_name=d["name"]))

    email_cards: List[Dict[str, Any]] = []
    for d in email_docs:
        email_cards.extend(parse_email_cards(d["text"], doc_name=d["name"]))

    all_event_cards = standup_cards + slack_cards + email_cards

    # LLM extract on event chunks -> collect texts per src (but don't add chunk items to final!)
    llm_texts_by_src: DefaultDict[str, List[str]] = defaultdict(list)

    if not args.skip_llm_extract and all_event_cards:
        event_chunks = make_event_chunks(all_event_cards, max_chars=args.max_chars, overlap=args.overlap)
        for ch in event_chunks:
            src = ch["src"]
            source = f"{src}:chunk{ch['chunk_id']}"
            try:
                res = llm_extract.extract_items(ch["text"], source=source)
                for it in (res.get("items") or []):
                    txt = (it.get("text") or "").strip()
                    if txt:
                        llm_texts_by_src[src].append(txt)
            except Exception as e:
                print(f"[warn] LLM extract failed on {source}: {e}")

    # Build ONE aggregated item per event
    items: List[Dict[str, Any]] = []

    for c in standup_cards:
        src = c.get("src")
        items.append(aggregate_standup_to_one_item(c, llm_texts_by_src.get(src, []), max_llm_notes=args.standup_llm_notes))

    for c in slack_cards:
        src = c.get("src")
        items.append(aggregate_slack_to_one_item(c, llm_texts_by_src.get(src, []), max_llm_notes=args.slack_llm_notes))

    for c in email_cards:
        src = c.get("src")
        items.append(aggregate_email_to_one_item(c, llm_texts_by_src.get(src, []), max_llm_notes=args.email_llm_notes))

    # LLM extraction on non-event docs (normal behavior)
    if not args.skip_llm_extract:
        for ch in chunks:
            source = f"{ch['doc_name']}:chunk{ch['chunk_id']}"
            try:
                res = llm_extract.extract_items(ch["text"], source=source)
                items.extend(res.get("items", []))
            except Exception as e:
                print(f"[warn] LLM extract failed on {source}: {e}")

    # Summarize (best-effort)
    try:
        _ = llm_summarize.summarize(items)
    except Exception:
        pass

    generated_iso = datetime.now().isoformat(timespec="seconds")
    report = build_report(items, generated_iso=generated_iso)

    # Debug outputs
    (out_dir / "standups.json").write_text(json.dumps(standup_cards, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "slack_events.json").write_text(json.dumps(slack_cards, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "email_events.json").write_text(json.dumps(email_cards, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown_compact(report), encoding="utf-8")
    (out_dir / "report.html").write_text(render_html_compact(report), encoding="utf-8")

    print("\n[ok] Outputs:")
    print(" -", out_dir / "report.md")
    print(" -", out_dir / "report.html")
    print(" -", out_dir / "report.json")
    print(" -", out_dir / "standups.json")
    print(" -", out_dir / "slack_events.json")
    print(" -", out_dir / "email_events.json")
    print(" -", out_dir / "items.json")


if __name__ == "__main__":
    main()
