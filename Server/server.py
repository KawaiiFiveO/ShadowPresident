import json
import math
import random
import os
import re
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

llm = OpenAI(
    base_url=config["lm_studio_url"] + "/v1",
    api_key=config.get("api_key", "not-needed"),
)


SYSTEM_PROMPT          = config["system_prompt"]
PROLOGUE_SYSTEM_PROMPT = config.get("prologue_system_prompt", SYSTEM_PROMPT)
ADVISOR_PROMPT         = config.get(
    "advisor_prompt",
    "You are an advisor watching Anton Rayne manage the country. "
    "Answer questions about the current game situation using the recent dialogue as context. "
    "Be concise and specific.",
)
MODEL           = config["model"]
PORT            = config.get("port", 1954)
MAX_TOKENS      = config.get("max_tokens", 1024)
TEMPERATURE     = config.get("temperature", 0.7)
DISPLAY_NAME    = config.get("display_name") or MODEL.split("/")[-1]
ENABLE_THINKING     = config.get("enable_thinking", False)
# The model's full context window (n_ctx in LM Studio). The dialogue-context budget is derived
# from this minus the system prompt, injected memory, the completion reservation (max_tokens), and
# a small overhead for choices/stats/codex/formatting — so we actually fill the window instead of
# guessing a static cap.
CONTEXT_WINDOW              = config.get("context_window", 8192)
PROMPT_OVERHEAD_TOKENS      = config.get("prompt_overhead_tokens", 400)
# Optional hard ceiling on the dialogue-context budget; None (omit from config) = derive purely
# from the window.
MAX_CONTEXT_TOKENS          = config.get("max_context_tokens")

# ── Run persistence ───────────────────────────────────────────────────────────
# last_run.json stores the active log file paths so the server can continue
# the same log across restarts (quit-and-resume on the same save).
# A new run is started when the first post-restart checkpoint shows a lower
# turn than the last checkpoint in the existing log (new game detection).

os.makedirs("logs", exist_ok=True)
LAST_RUN_META = "logs/last_run.json"

RUN_ID               = ""
_jsonl_path          = ""
_txt_path            = ""
_json_log            = None
_txt_log             = None
_last_checkpoint_turn = -1  # last turn seen, for display in /context

# Live turn/step/fragment from the most recent decision (for the panel header).
_cur_turn     = 0
_cur_step     = 0
_cur_fragment = ""

# TXT dedup state
_prev_context: list[str] = []
_prev_turn: int = -1

# Display-only transcript for the browser panel. Accumulates new dialogue lines
# incrementally (unlike _prev_context, which is the rolling LLM window replaced wholesale
# each decision) and gets a SCENE_BREAK sentinel inserted at every checkpoint so the panel
# can draw a divider between conversations. Never sent to the LLM.
SCENE_BREAK = "\x1e"           # ASCII record separator — won't collide with dialogue
#PANEL_CONTEXT_MAX = 500
_panel_context: list[str] = []

def _panel_append(lines: list[str]):
    if not lines:
        return
    _panel_context.extend(lines)
    #if len(_panel_context) > PANEL_CONTEXT_MAX:
    #    del _panel_context[:len(_panel_context) - PANEL_CONTEXT_MAX]

def _panel_scene_break():
    # Avoid consecutive/leading breaks (e.g. empty fragments).
    if _panel_context and _panel_context[-1] != SCENE_BREAK:
        _panel_append([SCENE_BREAK])

# Full dialogue accumulator for the current story fragment — reset at each checkpoint.
# Used for memory summaries so the whole conversation is captured, not just the rolling tail.
_fragment_context: list[str] = []

# Last decision token counts, game stats, injected codex entries, and raw request body
_last_prompt_tokens     = 0
_last_completion_tokens = 0
_last_stats             = ""
_last_news              = ""
_last_reports           = ""
_last_codex_injected: list[dict] = []  # [{title, summary}] from last decision
_last_codex_debug:   dict        = {}  # selection logic snapshot from last decision
_last_reports_debug: dict        = {}  # {selected: [...], all: [...]} from last decision
_last_raw_request:   dict        = {}  # full kwargs sent to LM Studio in last decision call

# Recent AI decisions for the reasoning log (ring buffer, AI only — no human entries)
MAX_RECENT_DECISIONS = 50
_recent_decisions: list[dict] = []

# Activity feed — recent server events (decisions, checkpoints, memory writes, codex caching)
_activity_log: list[dict] = []
_activity_lock = threading.Lock()
MAX_ACTIVITY   = 30

# Per-fragment codex relevance tracking — articy_id → ref count since last checkpoint.
# Reset at /checkpoint so injection priority reflects the current conversation.
_fragment_codex_refs: dict[str, int] = {}
# Last known state of the C# codex queue — used to diff and count only newly-seen IDs.
_prev_codex_refs: set[str] = set()

# Estimated token cost of the codex block in the last decision prompt (for budget breakdown)
_last_codex_tokens = 0

# Persistent memory — one line per notable event, injected into every system prompt
MEMORY_FILE           = "memory.txt"
MAX_MEMORY_LINES      = 100   # hard cap (FIFO fallback if compaction fails)
COMPACTION_THRESHOLD  = 75    # trigger compaction at this many lines
COMPACTION_KEEP_RECENT = 40   # always keep this many recent entries verbatim
_memory_lines: list[str] = []
_memory_lock = threading.Lock()
_compacting  = False


def _write_txt(text: str):
    _txt_log.write(text)
    _txt_log.flush()


def _log_activity(type_: str, desc: str):
    with _activity_lock:
        _activity_log.append({"type": type_, "time": datetime.now().strftime("%H:%M:%S"), "desc": desc})
        if len(_activity_log) > MAX_ACTIVITY:
            del _activity_log[:-MAX_ACTIVITY]


def _read_last_checkpoint(path: str) -> dict | None:
    """Scan a JSONL file and return the last checkpoint entry, or None."""
    try:
        last = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "checkpoint":
                        last = entry
                except Exception:
                    pass
        return last
    except Exception:
        return None


def _restore_context_from_jsonl(path: str) -> list[str]:
    """Return dialogue lines between the last checkpoint and EOF.

    Called on resume so _prev_context and _fragment_context start populated,
    preventing duplicate JSONL entries and giving the memory summarizer a full
    picture when the next checkpoint fires.
    """
    lines_since_checkpoint: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    if entry.get("type") == "checkpoint":
                        lines_since_checkpoint = []
                    elif entry.get("type") == "dialogue":
                        text = entry.get("text", "")
                        if text:
                            lines_since_checkpoint.append(text)
                except Exception:
                    pass
    except Exception as e:
        print(f"[run] Could not restore context: {e}", flush=True)
    return lines_since_checkpoint


def _open_log_files(jsonl_path: str, txt_path: str, run_id: str):
    global RUN_ID, _json_log, _txt_log, _jsonl_path, _txt_path
    RUN_ID      = run_id
    _jsonl_path = jsonl_path
    _txt_path   = txt_path
    _json_log   = open(jsonl_path, "a", encoding="utf-8")
    _txt_log    = open(txt_path,   "a", encoding="utf-8")


def _save_run_meta():
    try:
        with open(LAST_RUN_META, "w", encoding="utf-8") as f:
            json.dump({"run_id": RUN_ID, "jsonl": _jsonl_path, "txt": _txt_path}, f)
    except Exception as e:
        print(f"[run] Could not save run meta: {e}", flush=True)


def _start_new_run():
    global _last_checkpoint_turn, _prev_context, _prev_turn
    run_id    = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = DISPLAY_NAME.replace(" ", "_").replace("/", "-")
    base      = f"logs/run_{run_id}_{safe_name}"
    _open_log_files(f"{base}.jsonl", f"{base}.txt", run_id)
    _last_checkpoint_turn = -1
    _prev_context = []
    _prev_turn    = -1
    _panel_context.clear()
    _save_run_meta()
    print(f"[run] New run {run_id} → {_jsonl_path}", flush=True)


def _load_or_create_run():
    global _last_checkpoint_turn
    try:
        if os.path.exists(LAST_RUN_META):
            with open(LAST_RUN_META, encoding="utf-8") as f:
                meta = json.load(f)
            jp = meta.get("jsonl", "")
            tp = meta.get("txt", "")
            if os.path.exists(jp) and os.path.exists(tp):
                last_cp = _read_last_checkpoint(jp)
                cp_info = ""
                if last_cp:
                    cp_info = (f" — last checkpoint: Turn {last_cp.get('turn')}, "
                               f"{last_cp.get('fragment', '')}")
                    _last_checkpoint_turn = last_cp.get("turn", -1)

                print(f"\nPrevious run found: {meta['run_id']}{cp_info}")
                print("Continue this run? [y/n]: ", end="", flush=True)
                answer = input().strip().lower()

                if answer == "y":
                    _open_log_files(jp, tp, meta["run_id"])
                    _write_txt(
                        f"\n{'─' * 60}\n"
                        f"[SERVER RESTARTED — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n"
                        f"{'─' * 60}\n"
                    )
                    restored = _restore_context_from_jsonl(jp)
                    if restored:
                        _prev_context.extend(restored)
                        _fragment_context.extend(restored)
                        _panel_append(restored)
                        print(f"[run] Restored {len(restored)} context lines from JSONL.", flush=True)
                    print(f"[run] Continuing run {RUN_ID}", flush=True)
                    return

                print("[run] Starting fresh run", flush=True)
    except Exception as e:
        print(f"[run] Could not load previous run ({e}) — starting fresh", flush=True)
    _start_new_run()


_load_or_create_run()

# ── Memory helpers ────────────────────────────────────────────────────────────

def _load_memory():
    global _memory_lines
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, encoding="utf-8") as f:
                _memory_lines = [l.rstrip("\n") for l in f if l.strip()]
            print(f"[memory] Loaded {len(_memory_lines)} entries from {MEMORY_FILE}", flush=True)
        except Exception as e:
            print(f"[memory] Could not load {MEMORY_FILE}: {e}", flush=True)


def _save_memory():
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(_memory_lines) + "\n")
    except Exception as e:
        print(f"[memory] Write failed: {e}", flush=True)


def _append_memory(entry: str):
    global _compacting
    with _memory_lock:
        _memory_lines.append(entry)
        if len(_memory_lines) > MAX_MEMORY_LINES:
            del _memory_lines[:-MAX_MEMORY_LINES]
        _save_memory()
        should_compact = (not _compacting and len(_memory_lines) >= COMPACTION_THRESHOLD)
        if should_compact:
            _compacting = True
    print(f"[memory] {entry}", flush=True)
    if should_compact:
        threading.Thread(target=_run_compaction, daemon=True).start()


def _run_compaction():
    """Summarize the oldest memory entries to free space, preserving recent ones verbatim."""
    global _compacting
    try:
        with _memory_lock:
            n = len(_memory_lines)
            if n < COMPACTION_THRESHOLD:
                return
            to_compact = list(_memory_lines[:n - COMPACTION_KEEP_RECENT])
        if not to_compact:
            return

        print(f"[memory] Compacting {len(to_compact)} old entries…", flush=True)
        text    = "\n".join(to_compact)
        prompt  = (
            f"These are memory log entries from a game playthrough:\n{text}\n\n"
            "Compress these into 5–8 concise bullet points capturing the most important "
            "decisions and events. One sentence each. Start every bullet with '•'."
        )
        kwargs = dict(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You compress memory logs for a game playthrough. Be specific and factual."},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=2048,
            temperature=0.3,
        )

        response = llm.chat.completions.create(**kwargs)
        raw      = strip_think_tags(response.choices[0].message.content or "").strip()
        bullets  = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        summary  = [f"[Summary] {b.lstrip('•').strip()}" for b in bullets if b]

        with _memory_lock:
            # Keep entries added while the LLM was running (indices ≥ n)
            added_since = list(_memory_lines[n:])
            recent      = list(_memory_lines[n - COMPACTION_KEEP_RECENT:n])
            _memory_lines.clear()
            _memory_lines.extend(summary)
            _memory_lines.extend(recent)
            _memory_lines.extend(added_since)
            _save_memory()

        print(f"[memory] Compacted {len(to_compact)} → {len(summary)} lines", flush=True)
        _log_activity("compact", desc=f"Compacted {len(to_compact)} → {len(summary)} entries")
    except Exception as e:
        print(f"[memory] Compaction failed: {e}", flush=True)
    finally:
        _compacting = False


def _clear_memory():
    with _memory_lock:
        _memory_lines.clear()
        _save_memory()
    print("[memory] Cleared", flush=True)


_IMPORTANT_TYPES = {"bill", "paged_decision", "decree"}


def _generate_memory_entry(context: list[str], turn: int, fragment: str,
                            decision: dict | None = None):
    """Background thread: ask the LLM for a one-line memory entry.

    When `decision` is provided (for important decision types and prologue choices)
    the prompt includes explicit choice info so the summarizer doesn't have to
    infer it from context alone.
    """
    sys_prompt = "You write factual memory log entries for a game. Report only what happened. No reasoning, no motivation, no interpretation."

    if decision:
        choices     = decision.get("choices", [])
        choice_idx  = decision.get("choice_index", 0)
        dtype       = decision.get("decision_type", "")
        chosen_text = choices[choice_idx]["text"] if choice_idx < len(choices) else ""
        if not chosen_text:
            return
        type_label = {"bill": "Bill", "paged_decision": "Policy", "decree": "Decree"}.get(dtype, dtype)
        _append_memory(f"[Turn {turn}] {type_label}: {chosen_text}")
        _log_activity("memory", desc=f"[Turn {turn}] {type_label}: {chosen_text[:60]}")
        return
    elif context:
        if not any(l.startswith("[CHOICE]: ") for l in context):
            return
        context_text = "\n".join(context)
        prompt = (
            f"This is a transcript of a political scene:\n{context_text}\n\n"
            "Write a brief factual summary (1-3 sentences) of what happened and what decisions were made. "
            "State only what happened. No interpretation, no analysis."
        )
        sys_prompt = "You record events from a game playthrough. State only what happened. No interpretation, no analysis."
    else:
        return

    try:
        kwargs = dict(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=2048,
            temperature=0.3,
        )

        response = llm.chat.completions.create(**kwargs)
        raw = strip_think_tags(response.choices[0].message.content or "").strip().strip('"')
        if raw:
            _append_memory(f"[Turn {turn}] {raw}")
            _log_activity("memory", desc=f"[Turn {turn}] {raw[:80]}")
    except Exception as e:
        print(f"[memory] Generation failed: {e}", flush=True)


def _effective_system_prompt(phase: str) -> str:
    base = PROLOGUE_SYSTEM_PROMPT if phase == "prologue" else SYSTEM_PROMPT
    with _memory_lock:
        if not _memory_lines:
            return base
        memory_block = "\n".join(_memory_lines)
    return base + f"\n\n## Your history so far\n{memory_block}"


_load_memory()

# ── Log helpers ───────────────────────────────────────────────────────────────

def log_decision(entry: dict, context: list[str], choices: list[dict],
                 index: int, reasoning: str):
    global _prev_context, _prev_turn, _recent_decisions

    if entry.get("model_name") != "human" and reasoning:
        _recent_decisions.append({
            "turn":          entry.get("turn", 0),
            "fragment":      entry.get("fragment", ""),
            "decision_type": entry.get("decision_type", ""),
            "choices":       choices,
            "choice_index":  index,
            "reasoning":     reasoning,
            "prompt_tokens": entry.get("prompt_tokens", 0),
            "completion_tokens": entry.get("completion_tokens", 0),
            "timestamp":     entry.get("timestamp", ""),
        })
        if len(_recent_decisions) > MAX_RECENT_DECISIONS:
            del _recent_decisions[:-MAX_RECENT_DECISIONS]

    turn = entry.get("turn", 0)
    
    # FIX: Use the sequence overlap diff instead of string checking
    new_lines = _get_new_lines(_prev_context, context)
    
    _fragment_context.extend(new_lines)
    for line in new_lines:
        _json_log.write(json.dumps(
            {"type": "dialogue", "run_id": RUN_ID, "turn": turn, "text": line},
            ensure_ascii=False,
        ) + "\n")

    _json_log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _json_log.flush()

    if turn != _prev_turn:
        _write_txt(f"\n{'═' * 60}\n  TURN {turn}\n{'═' * 60}\n")
        _prev_turn = turn

    # (Removed the redundant second new_lines calculation here)
    _prev_context = list(context)
    _panel_append(new_lines)
    
    if new_lines:
        _write_txt("\n" + "\n".join(new_lines) + "\n")

    arrow_choices = "\n".join(
        f"  {'→' if c['index'] == index else ' '} {c['index']}. {c['text']}"
        for c in choices
    )
    chosen_text = choices[index]["text"] if index < len(choices) else "?"
    _write_txt(f"\n{arrow_choices}\n")
    _write_txt(f"\n[{DISPLAY_NAME}] → {chosen_text}\n")
    if reasoning:
        _write_txt(f'  "{reasoning}"\n')


def log_checkpoint(entry: dict):
    _json_log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _json_log.flush()
    turn, step = entry.get("turn", 0), entry.get("step", 0)
    _write_txt(f"\n{'─' * 60}\n[AUTOSAVE — Turn {turn}, Step {step}]\n{'─' * 60}\n")


# ── LLM helpers ───────────────────────────────────────────────────────────────

def est_tokens(text: str) -> int:
    """4 chars ≈ 1 token, the rough heuristic used throughout."""
    return len(text) // 4


def context_budget(phase: str = "main") -> int:
    """Tokens available for the dialogue context, after the base system prompt, injected memory,
    the completion reservation, and prompt overhead are subtracted from the model window.
    Capped by max_context_tokens if that is set in config."""
    base_sys = PROLOGUE_SYSTEM_PROMPT if phase == "prologue" else SYSTEM_PROMPT
    with _memory_lock:
        mem_tok = sum(est_tokens(l) for l in _memory_lines)
    budget = CONTEXT_WINDOW - MAX_TOKENS - est_tokens(base_sys) - mem_tok - PROMPT_OVERHEAD_TOKENS
    if MAX_CONTEXT_TOKENS is not None:
        budget = min(budget, MAX_CONTEXT_TOKENS)
    return max(0, budget)


def trim_context(context: list[str], budget: int) -> list[str]:
    """Drop oldest lines until the context fits within budget tokens."""
    lines = list(context)
    while lines and sum(est_tokens(l) for l in lines) > budget:
        lines.pop(0)
    return lines


def select_reports(reports_str: str) -> str:
    """Show all if ≤4 reports; otherwise randomly sample ceil(log2(n+1)) so large
    lists rotate across decisions rather than always showing the same first N."""
    if not reports_str:
        return ""
    lines = [l for l in reports_str.split('\n') if l.strip()]
    n = len(lines)
    if n == 0:
        return ""
    take = n if n <= 4 else max(3, math.ceil(math.log2(n + 1)))
    if take >= n:
        return '\n'.join(lines)
    return '\n'.join(random.sample(lines, take))


def build_prompt(decision_type: str, context: list[str], choices: list[dict],
                 stats: str = "", codex_refs: list[str] = None, phase: str = "main",
                 news: str = "", reports: str = "") -> str:
    trimmed      = trim_context(context, context_budget(phase))
    dropped      = len(context) - len(trimmed)
    prefix       = f"({dropped} older lines omitted)\n" if dropped else ""
    context_text = prefix + "\n".join(trimmed) if trimmed else "(No recent dialogue)"
    choices_text = "\n".join(f'{c["index"]}. {c["text"]}' for c in choices)
    selected_reports = select_reports(reports)

    # Snapshot which reports were shown to the AI vs the full available set (for the
    # last-prompt debug panel — mirrors the codex selection debug).
    global _last_reports_debug
    all_report_lines      = [l for l in reports.split('\n') if l.strip()]
    selected_report_lines = [l for l in selected_reports.split('\n') if l.strip()]
    _last_reports_debug = {"all": all_report_lines, "selected": selected_report_lines}

    type_labels = {
        "dialogue":       "Choose your response in the conversation",
        "bill":           "Decide whether to sign or veto this bill",
        "decision_panel": "Make your decision",
        "paged_decision": "Choose a policy option",
        "decree":         "Choose whether to sign a decree, and which one",
    }
    instruction = type_labels.get(decision_type, "Make your choice")

    major = decision_type in ("bill", "paged_decision", "decree")
    reasoning_hint = "2-3 sentences" if major else "one sentence"

    # Inject codex entries most-referenced since the last checkpoint, scaled to how many
    # unique entries have been seen in this fragment (more references → more injection).
    codex_block = ""
    global _last_codex_injected, _last_codex_tokens, _last_codex_debug
    _last_codex_tokens = 0
    if codex_refs and _fragment_codex_refs:
        unique_count  = len(_fragment_codex_refs)
        max_inject    = min(unique_count, 5)
        # Build a position map from the C# queue (oldest=0, newest=last index).
        # Used as a tie-breaker so more recently seen entries rank higher on equal counts.
        codex_order = {r: i for i, r in enumerate(codex_refs)}
        # All candidates: referenced this fragment AND summary cached
        all_candidates = sorted(
            [r for r in codex_order if r in _fragment_codex_refs and _codex.get(r, {}).get("summary")],
            key=lambda r: (_fragment_codex_refs[r], codex_order[r]),
            reverse=True,
        )
        scored = all_candidates[:max_inject]
        _last_codex_debug = {
            "unique_fragment_refs": unique_count,
            "max_inject":           max_inject,
            "injected":             [{"title": _codex[r].get("title", r), "count": _fragment_codex_refs[r]} for r in scored],
            "dropped":              [{"title": _codex[r].get("title", r), "count": _fragment_codex_refs[r]} for r in all_candidates[max_inject:]],
            "no_summary":           [_codex.get(r, {}).get("title", r) for r in codex_order if r in _fragment_codex_refs and not _codex.get(r, {}).get("summary")],
            "not_in_fragment":      [_codex.get(r, {}).get("title", r) if r in _codex else r for r in codex_order if r not in _fragment_codex_refs],
        }
        snippets = []
        for ref_id in scored:
            e = _codex[ref_id]
            snippets.append(f"- {e.get('title', ref_id)}: {e['summary']}")
        if snippets:
            codex_block = "\n".join(s.strip() for s in snippets)
            _last_codex_tokens = est_tokens(codex_block)
            _last_codex_injected = [
                {"title": _codex[r].get("title", r), "summary": _codex[r]["summary"]}
                for r in scored
            ]
            print(f"[codex] Injecting {len(snippets)}: " + ", ".join(e["title"] for e in _last_codex_injected), flush=True)
    else:
        _last_codex_injected = []
        _last_codex_debug = {
            "unique_fragment_refs": len(_fragment_codex_refs),
            "max_inject":           0,
            "injected":             [],
            "dropped":              [],
            "no_summary":           [],
            "not_in_fragment":      [_codex.get(r, {}).get("title", r) if r in _codex else r for r in set(codex_refs)] if codex_refs else [],
            "reason":               "No fragment refs tracked yet" if not _fragment_codex_refs else "No codex_refs from C#",
        }

    sections = [f"Recent dialogue:\n{context_text}"]
    if stats:            sections.append(f"Current game stats: {stats}")
    if news:             sections.append(f"Press:\n{news.strip()}")
    if selected_reports: sections.append(f"Reports:\n{selected_reports.strip()}")
    if codex_block:      sections.append(f"Relevant context:\n{codex_block}")

    return (
        "\n\n".join(sections) + "\n\n"
        f"{instruction}:\n{choices_text}\n\n"
        f'Respond with JSON only: {{"choice_index": N, "reasoning": "{reasoning_hint}"}}'
    )


def strip_think_tags(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def parse_response(content: str, num_choices: int) -> tuple[int, str]:
    clean = strip_think_tags(content)
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            index = max(0, min(int(data.get("choice_index", 0)), num_choices - 1))
            return index, str(data.get("reasoning", "")).strip()
        except (json.JSONDecodeError, ValueError):
            pass
    digit = re.search(r"\d+", clean)
    index = max(0, min(int(digit.group()), num_choices - 1)) if digit else 0
    return index, ""
    
def _get_new_lines(prev_ctx: list[str], current_ctx: list[str]) -> list[str]:
    """Finds new lines by matching the longest overlapping sequence of the rolling window."""
    max_overlap = min(len(prev_ctx), len(current_ctx))
    # Search from largest possible overlap down to 1
    for i in range(max_overlap, 0, -1):
        if prev_ctx[-i:] == current_ctx[:i]:
            return current_ctx[i:]
    # If there is no overlap at all (or if prev_ctx was empty), all lines are new
    return list(current_ctx)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/decision")
def decision():
    global _last_news, _last_reports, _cur_turn, _cur_step, _cur_fragment
    data          = request.get_json(force=True)
    decision_type = data.get("type", "dialogue")
    context       = data.get("context", [])
    choices       = data.get("choices", [])
    turn          = data.get("turn", 0)
    step          = data.get("step", 0)
    fragment      = data.get("fragment", "")
    phase         = data.get("phase", "main")
    stats      = data.get("stats", "")
    news       = data.get("news", "")
    reports    = data.get("reports", "") or _last_reports
    codex_refs = data.get("codex_refs", [])
    _cur_turn, _cur_step, _cur_fragment = turn, step, fragment
    if news:    _last_news    = news
    if reports: _last_reports = select_reports(reports)
    system_prompt = _effective_system_prompt(phase)

    if not choices:
        return jsonify({"choice_index": 0, "reasoning": "", "model_name": DISPLAY_NAME})

    # Count only IDs newly added to the C# queue since the last request (set diff).
    # The C# sends the full rolling queue every time, so a simple increment would
    # give +1 per decision call to every entry, making all counts identical.
    global _prev_codex_refs
    for ref_id in set(codex_refs) - _prev_codex_refs:
        _fragment_codex_refs[ref_id] = _fragment_codex_refs.get(ref_id, 0) + 1
    _prev_codex_refs = set(codex_refs)

    # Human choice pre-logged by the plugin — skip LLM, just write to logs.
    provided_index = data.get("choice_index", None)
    if provided_index is not None:
        index = max(0, min(int(provided_index), len(choices) - 1))
        entry = {
            "type":          "decision",
            "run_id":        RUN_ID,
            "timestamp":     datetime.now().isoformat(),
            "turn":          turn,
            "step":          step,
            "fragment":      fragment,
            "decision_type": decision_type,
            "phase":         phase,
            "choices":       choices,
            "choice_index":  index,
            "reasoning":     "",
            "model_name":    "human",
        }
        log_decision(entry, context, choices, index, "")
        chosen_text = choices[index]["text"] if index < len(choices) else "?"
        print(f"\n[human] Turn {turn} / {decision_type} → {index}: {chosen_text}", flush=True)
        _log_activity("decision", desc=f"Turn {turn} · {decision_type} → {chosen_text[:50]} (human)")
        return jsonify({"choice_index": index, "reasoning": "", "model_name": "human"})

    prompt = build_prompt(decision_type, context, choices, stats, codex_refs, phase, news, reports)

    try:
        kwargs = dict(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        if ENABLE_THINKING:
            kwargs["extra_body"] = {"enable_thinking": True}
        elif "response_format" in config:
            kwargs["response_format"] = config["response_format"]

        global _last_prompt_tokens, _last_completion_tokens, _last_stats, _last_raw_request
        _last_raw_request = dict(kwargs)

        response = llm.chat.completions.create(**kwargs)
        content  = response.choices[0].message.content or ""
        index, reasoning = parse_response(content, len(choices))
        if stats:
            _last_stats = stats
        usage         = response.usage
        prompt_tokens = usage.prompt_tokens     if usage else 0
        compl_tokens  = usage.completion_tokens if usage else 0
        _last_prompt_tokens     = prompt_tokens
        _last_completion_tokens = compl_tokens

        print(f"\n[{DISPLAY_NAME}] Turn {turn} / {decision_type}  [{prompt_tokens} in / {compl_tokens} out]", flush=True)
        print(f"  → {index}: {choices[index]['text']}", flush=True)
        if reasoning:
            print(f"  \"{reasoning}\"", flush=True)

        entry = {
            "type":             "decision",
            "run_id":           RUN_ID,
            "timestamp":        datetime.now().isoformat(),
            "turn":             turn,
            "step":             step,
            "fragment":         fragment,
            "decision_type":    decision_type,
            "phase":            phase,
            "choices":          choices,
            "choice_index":     index,
            "reasoning":        reasoning,
            "prompt_tokens":    prompt_tokens,
            "completion_tokens": compl_tokens,
        }
        log_decision(entry, context, choices, index, reasoning)
        chosen_desc = choices[index]["text"] if index < len(choices) else "?"
        _log_activity("decision", desc=f"Turn {turn} · {decision_type} → {chosen_desc[:60]}")

        # Generate a memory entry immediately for major in-game decisions.
        # Prologue choices accumulate in _fragment_context and are summarised at checkpoint.
        if decision_type in _IMPORTANT_TYPES:
            dec_snapshot = {
                "decision_type": decision_type,
                "choices":       choices,
                "choice_index":  index,
                "reasoning":     reasoning,
            }
            threading.Thread(
                target=_generate_memory_entry,
                args=([], turn, fragment),
                kwargs={"decision": dec_snapshot},
                daemon=True,
            ).start()

        return jsonify({
            "choice_index":      index,
            "reasoning":         reasoning,
            "model_name":        DISPLAY_NAME,
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": compl_tokens,
        })

    except Exception as exc:
        print(f"[AI error] {exc}", flush=True)
        return jsonify({"error": str(exc)}), 500


@app.post("/stats")
def post_stats():
    global _last_stats, _last_news, _last_reports
    data = request.get_json(force=True)
    stats   = data.get("stats",   "")
    news    = data.get("news",    "")
    reports = data.get("reports", "")
    if stats:   _last_stats   = stats
    if news:    _last_news    = news
    if reports: _last_reports = select_reports(reports)
    return jsonify({"ok": True})


@app.post("/checkpoint")
def checkpoint():
    global _prev_context, _pending_run_check, _last_checkpoint_turn

    data          = request.get_json(force=True)
    turn          = data.get("turn", 0)
    step          = data.get("step", 0)
    fragment      = data.get("fragment", "")
    context       = data.get("context", [])

    _last_checkpoint_turn = turn

    # FIX: Use the sequence overlap diff
    new_lines = _get_new_lines(_prev_context, context)
    
    _fragment_context.extend(new_lines)
    for line in new_lines:
        _json_log.write(json.dumps(
            {"type": "dialogue", "run_id": RUN_ID, "turn": turn, "text": line},
            ensure_ascii=False,
        ) + "\n")
        
    if new_lines:
        _json_log.flush()
        _write_txt("\n" + "\n".join(new_lines) + "\n")
        
    _prev_context = list(context)
    _panel_append(new_lines)
    _panel_scene_break()

    entry = {
        "type":      "checkpoint",
        "run_id":    RUN_ID,
        "timestamp": datetime.now().isoformat(),
        "turn":      turn,
        "step":      step,
        "fragment":  fragment,
    }
    log_checkpoint(entry)
    _save_run_meta()
    print(f"[checkpoint] Turn {turn} / Step {step} — {fragment}", flush=True)
    _fragment_codex_refs.clear()
    _log_activity("checkpoint", desc=f"Turn {turn} Step {step} · {fragment}")

    ctx_snapshot = list(_fragment_context)
    _fragment_context.clear()

    threading.Thread(
        target=_generate_memory_entry,
        args=(ctx_snapshot, turn, fragment),
        daemon=True,
    ).start()

    return jsonify({"status": "ok"})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "run_id": RUN_ID, "model": DISPLAY_NAME})


@app.post("/quit")
def quit_server():
    def _shutdown():
        import time as _time
        _time.sleep(0.3)   # let the response reach the browser first
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"status": "shutting down"})


CODEX_FILE = "codex.json"
_codex: dict[str, dict] = {}   # articy_id → {title, summary}

def _load_codex():
    global _codex
    if os.path.exists(CODEX_FILE):
        try:
            with open(CODEX_FILE, encoding="utf-8") as f:
                _codex = json.load(f)
            print(f"[codex] Loaded {len(_codex)} entries", flush=True)
        except Exception as e:
            print(f"[codex] Could not load {CODEX_FILE}: {e}", flush=True)

def _save_codex():
    try:
        with open(CODEX_FILE, "w", encoding="utf-8") as f:
            json.dump(_codex, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[codex] Save failed: {e}", flush=True)

CODEX_SUMMARY_MAX_CHARS = 400

def _make_codex_summary(raw: str) -> str:
    """Truncate raw codex text to a prompt-friendly length."""
    if not raw or len(raw) <= CODEX_SUMMARY_MAX_CHARS:
        return raw
    truncated = raw[:CODEX_SUMMARY_MAX_CHARS]
    for sep in (". ", "! ", "? "):
        pos = truncated.rfind(sep)
        if pos > CODEX_SUMMARY_MAX_CHARS // 2:
            return truncated[:pos + 1]
    return truncated.rsplit(" ", 1)[0] + "…"


_load_codex()

# Backfill any entries cached before this change with empty summaries.
_backfilled = 0
for _entry in _codex.values():
    if not _entry.get("summary") and _entry.get("raw"):
        _entry["summary"] = _make_codex_summary(_entry["raw"])
        _backfilled += 1
if _backfilled:
    _save_codex()
    print(f"[codex] Backfilled {_backfilled} empty summaries", flush=True)


@app.post("/codex")
def add_codex_entry():
    data      = request.get_json(force=True)
    articy_id = data.get("articy_id", "").strip()
    title     = data.get("title", "").strip()
    raw       = data.get("raw", "").strip()
    name_db   = data.get("name_in_db", "").strip()

    if not articy_id or not title:
        return jsonify({"status": "skipped"}), 200

    if articy_id in _codex:
        return jsonify({"status": "cached", "title": title}), 200

    summary = _make_codex_summary(raw)
    _codex[articy_id] = {"title": title, "name_in_db": name_db, "raw": raw, "summary": summary}
    _save_codex()
    print(f"[codex] New entry: {title}", flush=True)
    _log_activity("codex", desc=f"Cached: {title}")
    return jsonify({"status": "cached", "title": title}), 200


@app.get("/codex")
def get_codex():
    return jsonify({"entries": len(_codex), "codex": _codex})


@app.get("/codex/ids")
def get_codex_ids():
    return jsonify({"ids": list(_codex.keys())})


@app.get("/memory")
def get_memory():
    with _memory_lock:
        entries = list(_memory_lines)
    return jsonify({"lines": len(entries), "entries": entries, "compacting": _compacting})


@app.delete("/memory")
def delete_memory():
    _clear_memory()
    return jsonify({"status": "cleared"})


@app.get("/activity")
def get_activity():
    with _activity_lock:
        entries = list(reversed(_activity_log))
    return jsonify({"activity": entries})


@app.get("/context")
def context():
    est_context_tokens = sum(est_tokens(l) for l in _prev_context)
    with _memory_lock:
        est_memory_tokens = sum(est_tokens(l) for l in _memory_lines)
    return jsonify({
        "model":                  DISPLAY_NAME,
        "run_id":                 RUN_ID,
        "turn":                   _cur_turn,
        "step":                   _cur_step,
        "fragment":               _cur_fragment,
        "lines":                  len(_prev_context),
        "context":                _panel_context,
        "last_prompt_tokens":     _last_prompt_tokens,
        "last_completion_tokens": _last_completion_tokens,
        "max_tokens":             MAX_TOKENS,
        "context_window":         CONTEXT_WINDOW,
        "context_budget":         context_budget("main"),
        "est_context_tokens":     est_context_tokens,
        "est_memory_tokens":      est_memory_tokens,
        "est_sysprompt_tokens":   est_tokens(SYSTEM_PROMPT),
        "est_codex_tokens":       _last_codex_tokens,
        "est_news_tokens":        est_tokens(_last_news)    if _last_news    else 0,
        "est_reports_tokens":     est_tokens(_last_reports) if _last_reports else 0,
        "est_overhead_tokens":    PROMPT_OVERHEAD_TOKENS,
        "stats":                  _last_stats,
        "news":                   _last_news,
        "reports":                _last_reports,
        "last_codex_injected":    _last_codex_injected,
    })


@app.get("/decisions")
def decisions():
    return jsonify({"decisions": list(reversed(_recent_decisions))})


@app.get("/last-prompt")
def last_prompt():
    return jsonify({**_last_raw_request,
                    "codex_debug":   _last_codex_debug,
                    "reports_debug": _last_reports_debug})




@app.get("/")
def panel():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
    with open(path, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/ask")
def ask():
    data     = request.get_json(force=True)
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    context_text = "\n".join(_prev_context) if _prev_context else "(No recent dialogue)"
    prompt = f"Recent dialogue:\n{context_text}\n\nQuestion: {question}"

    with _memory_lock:
        advisor = ADVISOR_PROMPT + ("\n\nYour history so far:\n" + "\n".join(_memory_lines) if _memory_lines else "")

    try:
        kwargs = dict(
            model=MODEL,
            messages=[
                {"role": "system", "content": advisor},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        if ENABLE_THINKING:
            kwargs["extra_body"] = {"enable_thinking": True}

        response = llm.chat.completions.create(**kwargs)
        content  = strip_think_tags(response.choices[0].message.content or "")
        usage    = response.usage
        return jsonify({
            "answer":            content,
            "prompt_tokens":     usage.prompt_tokens     if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
        })
    except Exception as exc:
        print(f"[ask error] {exc}", flush=True)
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    print(f"Shadow President AI server — {DISPLAY_NAME} — port {PORT}", flush=True)
    app.run(host="127.0.0.1", port=PORT, debug=False)
