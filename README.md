# TaskDigest

TaskDigest turns noisy operational messages (Email, Slack, Standups) into a **clear, manager-ready daily summary** — with **deterministic priorities**, visible risks, and **zero data leakage**.

---

## What problem it solves

In real operations:

- Important risks are buried in long email threads
- Slack discussions require reading full context to understand decisions
- Standups repeat the same blockers across days
- After a vacation or context switch, onboarding is slow and painful

**Result:** missed risks, delayed decisions, cognitive overload.

---

## What TaskDigest does

TaskDigest ingests raw operational text and produces a compact report with:

- 🔥 **P0 / P1 / P2 priorities** (deterministic, policy-based)
- 💰 **Financial risks** highlighted automatically
- 🚫 **Blockers** that require action
- 📌 A short **Manager Summary** (LLM-generated, optional)
- 🧾 Full traceability: every item links back to its source

Outputs are available as **Markdown + HTML + JSON**.

---

## Key principles (why this is different)

### 1) LLM is used for synthesis, not decisions

- Priority is assigned by **explicit policy rules**
- LLM never decides what is critical
- If AI is wrong or unavailable — the system still works

### 2) Graceful degradation

- `--llm none` → deterministic pipeline only  
- `--llm ollama` → adds extraction + summaries  
- Same outputs, same structure

### 3) Full traceability

raw input → extracted items → policy priority → final report


Every item keeps:

- source file
- chunk id
- original wording

### 4) Privacy by design

- Runs locally
- No API keys
- No data leaves your machine

---

## Supported inputs

TaskDigest works with plain files (no integrations required):

inputs_demo/
├── email.txt # raw emails (multiple threads)
├── slack.txt # Slack messages + replies
└── standup.txt # structured standups


Supported formats:

- `.txt`
- `.md`
- `.json`

---

## Standup handling (important)

Standups are **not rendered as a separate section**.

Instead:

- Only **BLOCKERS** and **RISKS** are extracted
- They become normal operational items
- They appear once, in **P0 / P1 / P2** columns
- No repetition across report sections

This avoids duplicated noise and keeps focus on action.

---

## Output structure

After running, TaskDigest generates:

outputs/
├── report.md # compact manager-readable report
├── report.html # same content, visual layout
├── report.json # structured report data
├── items.json # all extracted items
└── standups.json # parsed standup cards (debug / audit)


---

## Priority logic (deterministic)

### P0 — Critical
- Money mismatch (settlement, ledger, payouts)
- Financial risk + urgency (EOD, delay today)
- Compliance-impacting incidents

### P1 — Medium
- SLA degradation
- Fraud / abuse signals
- Investigations blocked by missing access

### P2 — Low
- Planning
- Informational updates
- Routine execution

> **LLM suggestions cannot downgrade policy priority.**

---
## Requirements
Python 3.9+
macOS or Linux
(Windows: use WSL or follow manual steps) 


## How to run

chmod +x install.sh
./install.sh

### 1) With local LLM (Ollama)

Requirements:
- Ollama running locally
- No API keys

```bash
ollama serve
ollama pull phi3:mini

python main.py \
  --input inputs_demo \
  --output outputs \
  --llm ollama
```
### 2) Without AI (fully deterministic)

python main.py --input inputs_demo --output outputs --llm none


Example result (high level)

🔥 P0

Settlement mismatch (+18,420 AED) may delay merchant payouts today

🟡 P1

KYC vendor latency spike may impact onboarding SLA

Investigation blocked due to missing fraud dashboard access

Abnormal affiliate traffic (+35%)

🟢 P2

UI release moved to Friday (planning)

A manager can understand the situation in under 10 seconds.

Why this fits real operations

Built from real emails, Slack threads, and standups

Matches how ops, risk, and product teams actually communicate

Designed for:

Operations & management

Risk & compliance teams

Workflow automation pipelines

Next steps (future work)

Slack / Email API connectors

Incremental daily runs

Owner assignment & follow-ups

UI dashboard

Company-wide policy customization