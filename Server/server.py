import json
import math
import random
import os
import re
import shutil
import socket
import threading
import time
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
# Master switch for the Cloudflare tunnel view. True (default) → internet traffic gets the
# read-only panel; False → the tunnel is refused entirely and the server is local-only.
ALLOW_REMOTE_VIEW = config.get("allow_remote_view", True)
# How much slower the remote (tunnel) panel polls vs. the local view. 1 = same as local.
REMOTE_POLL_SCALE = config.get("remote_poll_scale", 4)
# Bind selection. An explicit "host" in config wins — set it to this machine's physical LAN IP
# (e.g. "192.168.1.50") so the listener lives on the LAN NIC only. Binding "0.0.0.0" listens on
# EVERY interface, which includes a VPN adapter (Mullvad/WireGuard) whose firewall then captures
# or blocks the return path even with local-network passthrough on. Binding a specific LAN IP
# keeps the socket off the tunnel entirely.
if config.get("host"):
    HOST = config["host"]
elif config.get("lan_access", False):
    HOST = "0.0.0.0"
else:
    HOST = "127.0.0.1"


def _local_ipv4_addresses():
    """Best-effort list of this machine's IPv4 addresses, labeled with a guess at LAN vs VPN.

    Note: the usual 'UDP-connect to 8.8.8.8 then getsockname()' trick returns the VPN adapter's
    IP while the tunnel is up (default route goes through it) — exactly the wrong answer here —
    so we enumerate all bound addresses instead and let the operator pick.
    """
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    labeled = []
    for ip in sorted(addrs):
        if ip.startswith("127."):
            tag = "loopback"
        elif ip.startswith(("192.168.", "10.")) or ip.startswith(tuple(f"172.{n}." for n in range(16, 32))):
            tag = "private/LAN"
        else:
            tag = "other (VPN?)"
        labeled.append((ip, tag))
    return labeled
MAX_TOKENS      = config.get("max_tokens", 4096)
TEMPERATURE     = config.get("temperature", 0.7)
SUMMARY_TEMPERATURE = config.get("summary_temperature", 0.7)
DISPLAY_NAME    = config.get("display_name") or MODEL.split("/")[-1]
ENABLE_THINKING     = config.get("enable_thinking", False)
# When thinking is on globally, summary generation (memory entries + compaction) can opt out of
# the <think> block — those outputs are clipped/compressed anyway, so the reasoning is wasted
# tokens and latency. Only consulted when ENABLE_THINKING is true; defaults to True (no change).
# Sent as chat_template_kwargs — the documented Qwen3.5 lever (its chat template exposes an
# enable_thinking switch). Whether it takes effect depends on the SERVING BACKEND forwarding
# chat_template_kwargs into the template: vLLM does; LM Studio's engine is the open question —
# watch a checkpoint's server log to confirm the summary has no <think> block. If the backend
# does NOT forward it, the only fallbacks are global and also disable thinking for decisions: a
# template edit ({%- set enable_thinking = false %} on line 1) or ollama's --think=false.
# strip_think_tags removes the block regardless, so an unforwarded flag costs tokens/latency,
# never correctness.
SUMMARY_ENABLE_THINKING = config.get("summary_enable_thinking", True)
# When the model returns a reply we can't extract a choice from (no JSON, no bare number), re-query
# the LLM up to this many extra times before giving up and falling back to the default choice.
PARSE_RETRIES       = config.get("parse_retries", 2)
# The model's full context window (n_ctx in LM Studio). The dialogue-context budget is derived
# from this minus the system prompt, injected memory, the completion reservation (max_tokens), and
# a small overhead for choices/stats/codex/formatting — so we actually fill the window instead of
# guessing a static cap.
CONTEXT_WINDOW              = config.get("context_window", 8192)
PROMPT_OVERHEAD_TOKENS      = config.get("prompt_overhead_tokens", 600)
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
# Serializes appends to the JSONL log. Manifesto rows are written from the checkpoint worker
# thread (revision) and the decision thread (draft), so writes must not interleave with the
# main request thread's dialogue/decision/checkpoint rows.
_json_lock           = threading.Lock()
_last_checkpoint_turn = -1  # last turn seen, for display in /context
_journal_logged_turn  = -1  # last turn the (long, cumulative) ledger was written to the JSONL

# Resume support. The checkpoint is the transaction commit: the game autosave ≡ the last
# `checkpoint` line in the JSONL. On resume the game replays everything after that autosave, so
# every post-checkpoint JSONL entry is a stale duplicate. We record the byte offset of the file
# *through* the last checkpoint, truncate to it on resume, and seed the rolling window from the
# dialogue leading up to it.
_last_checkpoint_offset  = 0      # byte size of the JSONL through the last flushed checkpoint
_memory_snapshot_path    = ""     # per-run memory.txt snapshot, restored on resume
_manifesto_snapshot_path = ""     # per-run manifesto.txt snapshot, restored on resume
_resuming                = False  # True when continuing a previous run (drives context restore)

# Idempotency cache for /decision. The client (AIClient.RequestDecision) retries a request on a
# transient socket abort, but the abort can fire *after* the server already ran the LLM and logged
# the decision — so a blind resend would log a second, duplicate decision row. The client sends a
# stable `request_id` (one GUID per decision, reused across all retry attempts); we cache the
# response keyed by that id and replay it verbatim on a duplicate, skipping the LLM call, the log
# write, and all per-request state mutation. Bounded FIFO so memory can't grow unbounded.
_decision_cache       = {}                  # request_id -> response payload
_decision_cache_order = []                  # FIFO of request_ids, oldest first
_decision_cache_lock  = threading.Lock()
_DECISION_CACHE_MAX   = 64

# Serializes the *processing* of /decision requests. The response cache above only catches a retry
# that arrives after the original already finished and cached. But the client's socket abort fires
# while the original is still running in the LLM — so the retry would otherwise find nothing cached
# and launch a SECOND concurrent LLM call (LM Studio queues it, doubling the work and, at temp > 0,
# risking a divergent answer + a duplicate decision row). Holding this lock across the whole
# decision means a retry blocks until the original completes, then finds the cached result the
# instant it acquires the lock. The game makes one decision at a time, so this never serializes
# genuinely independent work — the only waiters are duplicate retries of the same decision.
_decision_proc_lock   = threading.Lock()

def _decision_cache_get(request_id):
    if not request_id:
        return None
    with _decision_cache_lock:
        return _decision_cache.get(request_id)

def _decision_cache_put(request_id, payload):
    if not request_id:
        return
    with _decision_cache_lock:
        if request_id not in _decision_cache:
            _decision_cache_order.append(request_id)
            while len(_decision_cache_order) > _DECISION_CACHE_MAX:
                _decision_cache.pop(_decision_cache_order.pop(0), None)
        _decision_cache[request_id] = payload

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
_last_gen_seconds       = 0.0      # wall-clock generation time of the last decision's final attempt
_last_tok_per_sec       = 0.0      # completion_tokens / gen_seconds for that attempt (0 if unmeasurable)
# Serializes every LLM request. The backend serves one model, so concurrent calls (a background
# summary/compaction/manifesto revision overlapping a decision) queue at the server anyway — but
# that queue-wait would inflate a decision's measured gen_seconds and corrupt the tok/s gauge for
# both the overlapped decision and the one after. Holding this lock around each create() keeps the
# timed window contention-free so the speedometer reflects real generation speed.
_llm_lock               = threading.Lock()
_last_stats             = ""
_last_news              = ""        # windowed subset actually sent to the LLM
_last_reports           = ""
# Newspaper rotating window. C# now sends the FULL current-turn article set; the server picks
# the window the AI sees each decision (moved here so the panel can grey the un-sent articles).
_last_news_full         = ""        # full current-turn article list (all articles), for the panel
_last_news_debug: dict  = {}        # {all: [...], sent: [...]} from the last decision
_news_turn              = -1        # turn the news cursor is tracking
_news_offset            = 0         # cursor position; advances per AI decision, resets on new turn
ARTICLES_PER_READ       = 2         # articles surfaced to the AI per decision
# Journal ledger (system-prompt block) and economy trajectory (user-block tail) from the last
# decision. Persisted across requests like _last_stats so they survive a prologue/empty payload.
_last_journal           = ""
_last_economy           = ""
_last_codex_injected: list[dict] = []  # [{title, summary}] from last decision
_last_codex_debug:   dict        = {}  # selection logic snapshot from last decision
_last_reports_debug: dict        = {}  # {selected: [...], all: [...]} from last decision
_last_raw_request:   dict        = {}  # full kwargs sent to LM Studio in last decision call

# Recent AI decisions for the reasoning log (ring buffer, AI only — no human entries)
MAX_RECENT_DECISIONS = 100
_recent_decisions: list[dict] = []

# Activity feed — recent server events (decisions, checkpoints, memory writes, codex caching)
_activity_log: list[dict] = []
_activity_lock = threading.Lock()
MAX_ACTIVITY   = 50

# Per-fragment codex relevance tracking — articy_id → ref count since last checkpoint.
# Reset at /checkpoint so injection priority reflects the current conversation.
_fragment_codex_refs: dict[str, int] = {}
# Last known state of the C# codex queue — used to diff and count only newly-seen IDs.
_prev_codex_refs: set[str] = set()

# Estimated token cost of the codex block in the last decision prompt (for budget breakdown)
_last_codex_tokens = 0

# Persistent memory — Anton's subjective judgment, injected into every system prompt. The ledger
# (journal) now carries the facts, so memory is capped by an estimated TOKEN budget rather than a
# line count: a terse anchor and a 3-sentence judgment shouldn't count the same. Compaction
# summarises the oldest entries once memory exceeds the ceiling, keeping the most recent
# KEEP_RECENT tokens verbatim. Target ~3k tokens (~7% of the 40k window) — bigger isn't better
# (lost-in-the-middle + small-model drift); the extra window goes to dialogue context + ledger.
MEMORY_FILE               = "memory.txt"
MEMORY_ARCHIVE_FILE       = "memory_archive.txt"  # overflow when compaction can't run — nothing is ever lost
MEMORY_TOKEN_CEILING      = 4000   # compact when estimated memory tokens exceed this
MEMORY_KEEP_RECENT_TOKENS = 2400   # keep this many recent tokens verbatim through a compaction
MEMORY_HARD_LINE_CAP      = 200    # absolute FIFO backstop if compaction can't run (LLM down)
MEMORY_COMPACT_ATTEMPTS   = 3      # LLM tries before giving up and FIFO-truncating instead
_memory_lines: list[str] = []
_memory_lock = threading.Lock()
_compacting  = False
_memory_saved_mtime = 0.0   # mtime of memory.txt the last time *the server* wrote it; a differing
                            # on-disk mtime means a human hand-edited the file (see _reload_memory_if_edited)

# Presidential Manifesto — the AI's living strategy doc. Drafted at the prologue→turn-1
# boundary (first phase=main decision) and revised once per turn at checkpoints. Injected
# into the main system prompt between the persona/rules and memory (volatility ordering).
MANIFESTO_FILE   = "manifesto.txt"
# Mode selector:
#   "ai"    — the AI drafts at turn 1 and revises each turn (the original behaviour)
#   "human" — manifesto.txt is authored by you and is read-only to the server: injected into
#             every main-phase prompt, never drafted, revised, snapshotted or overwritten.
#             Edits to the file land at the next checkpoint (see _reload_authored_manifesto).
#   "off"   — no manifesto at all; nothing read, written, injected or snapshotted.
# Back-compat: absent `manifesto_mode` falls back to the older `enable_manifesto` bool.
MANIFESTO_MODE = str(config.get(
    "manifesto_mode",
    "ai" if config.get("enable_manifesto", True) else "off",
)).strip().lower()
if MANIFESTO_MODE not in ("ai", "human", "off"):
    print(f"[manifesto] Unknown manifesto_mode {MANIFESTO_MODE!r} — falling back to 'ai'.", flush=True)
    MANIFESTO_MODE = "ai"
MANIFESTO_ENABLED  = MANIFESTO_MODE != "off"
# True → the manifesto is human-written; every AI write path is a no-op.
MANIFESTO_AUTHORED = MANIFESTO_MODE == "human"
# Completion budget for drafting/revising the manifesto. Must be generous: a reasoning model
# (R1/QwQ distills) spends most of the budget inside <think>, and 1024 left the four sections
# truncated — or cut the model off mid-<think>, leaving an unclosed block that can't be stripped.
MANIFESTO_MAX_TOKENS = config.get("manifesto_max_tokens", 4096)
_manifesto         = ""
_manifesto_lock    = threading.Lock()
_manifesto_turn    = 0   # last turn the manifesto was drafted/revised for (0 = not yet drafted)
_manifesto_busy    = False  # guards against concurrent draft/revise calls
_manifesto_version = 0   # monotonic version label for the logged history (export)
_manifesto_mtime   = 0.0 # mtime of the last read of MANIFESTO_FILE ("human" mode reload check)


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


def _last_checkpoint_byte_offset(path: str) -> int | None:
    """Return the byte offset of the end of the last checkpoint line, or None.

    Used as a fallback when last_run.json predates checkpoint-offset recording.
    Reads in binary so the returned position is a true byte offset usable with truncate().
    """
    try:
        offset = None
        pos = 0
        with open(path, "rb") as f:
            for raw in f:
                pos += len(raw)
                try:
                    entry = json.loads(raw.decode("utf-8").strip())
                    if entry.get("type") == "checkpoint":
                        offset = pos
                except Exception:
                    pass
        return offset
    except Exception:
        return None


def _restore_context_from_jsonl(path: str) -> list[str]:
    """Return the rolling dialogue window *through* the last checkpoint, trimmed to budget.

    Called on resume after the file has been truncated to the last checkpoint, so every
    dialogue line in the file belongs to the committed state the game will replay up to.
    We collect them all (the conversation leading into the autosave) and trim to the
    context budget so _prev_context starts populated with the genuine rolling window —
    not context-starved as it was before.
    """
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    if entry.get("type") == "dialogue":
                        text = entry.get("text", "")
                        if text:
                            lines.append(text)
                except Exception:
                    pass
    except Exception as e:
        print(f"[run] Could not restore context: {e}", flush=True)
    return trim_context(lines, context_budget())


def _open_log_files(jsonl_path: str, txt_path: str, run_id: str):
    global RUN_ID, _json_log, _txt_log, _jsonl_path, _txt_path, _memory_snapshot_path, \
        _manifesto_snapshot_path
    RUN_ID      = run_id
    _jsonl_path = jsonl_path
    _txt_path   = txt_path
    stem = jsonl_path[:-6] if jsonl_path.endswith(".jsonl") else jsonl_path
    _memory_snapshot_path    = stem + ".memory.txt"
    _manifesto_snapshot_path = stem + ".manifesto.txt"
    _json_log   = open(jsonl_path, "a", encoding="utf-8")
    _txt_log    = open(txt_path,   "a", encoding="utf-8")


def _save_run_meta():
    try:
        with open(LAST_RUN_META, "w", encoding="utf-8") as f:
            json.dump({
                "run_id":            RUN_ID,
                "jsonl":             _jsonl_path,
                "txt":               _txt_path,
                "memory_snapshot":   _memory_snapshot_path,
                "manifesto_snapshot": _manifesto_snapshot_path,
                "manifesto_turn":    _manifesto_turn,
                "checkpoint_offset": _last_checkpoint_offset,
            }, f)
    except Exception as e:
        print(f"[run] Could not save run meta: {e}", flush=True)


def _start_new_run():
    global _last_checkpoint_turn, _prev_context, _prev_turn, _last_checkpoint_offset, \
        _manifesto, _manifesto_turn
    run_id    = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = DISPLAY_NAME.replace(" ", "_").replace("/", "-")
    base      = f"logs/run_{run_id}_{safe_name}"
    _open_log_files(f"{base}.jsonl", f"{base}.txt", run_id)
    _last_checkpoint_turn   = -1
    _last_checkpoint_offset = 0
    _prev_context = []
    _prev_turn    = -1
    _panel_context.clear()
    # Fresh playthrough → fresh manifesto (drafted at turn 1). Clear any leftover from a prior
    # run so the turn-1 draft trigger fires instead of injecting a stale plan. In "human" mode
    # the manifesto is the author's standing plan and outlives any single run — keep it.
    if not MANIFESTO_AUTHORED:
        _manifesto      = ""
        _manifesto_turn = 0
        _save_manifesto()
    _save_run_meta()
    print(f"[run] New run {run_id} → {_jsonl_path}", flush=True)


def _load_or_create_run():
    """Decide continue-vs-new and reconstruct server state *as of* the last checkpoint.

    On continue we truncate the JSONL to the last checkpoint (discarding the stale
    post-checkpoint entries the game will replay) and restore the memory snapshot. The
    rolling context window is seeded later by _restore_resume_context(), after memory is
    loaded so the budget calculation is correct.
    """
    global _last_checkpoint_turn, _last_checkpoint_offset, _resuming, _manifesto_turn
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
                    # Truncate the JSONL to the last checkpoint so post-checkpoint entries
                    # (which the game replays) don't get duplicated on the next decision.
                    offset = meta.get("checkpoint_offset")
                    if not offset:
                        offset = _last_checkpoint_byte_offset(jp)
                    if offset:
                        try:
                            with open(jp, "r+b") as f:
                                f.truncate(offset)
                            print(f"[run] Truncated JSONL to checkpoint offset {offset}.", flush=True)
                        except Exception as e:
                            print(f"[run] Could not truncate to checkpoint: {e}", flush=True)
                    _last_checkpoint_offset = offset or 0

                    # Restore the memory snapshot taken at the last checkpoint so memory is
                    # consistent with the committed state even after a non-clean exit — UNLESS
                    # memory.txt has been hand-edited since the snapshot was written. A human
                    # who edited the live file (while the server was stopped) is deliberately
                    # overriding memory; restoring the snapshot would silently undo that. Prefer
                    # the newer file by mtime, mirroring the manifesto "human" reasoning above.
                    snap = meta.get("memory_snapshot", "")
                    if snap and os.path.exists(snap):
                        try:
                            edited = (os.path.exists(MEMORY_FILE) and
                                      os.path.getmtime(MEMORY_FILE) > os.path.getmtime(snap))
                            if edited:
                                print(f"[run] memory.txt is newer than the snapshot — keeping your "
                                      f"hand-edited memory, snapshot restore skipped.", flush=True)
                            else:
                                shutil.copyfile(snap, MEMORY_FILE)
                                print(f"[run] Restored memory snapshot from {snap}.", flush=True)
                        except Exception as e:
                            print(f"[run] Could not restore memory snapshot: {e}", flush=True)

                    # Skipped in "human" mode — restoring a snapshot would overwrite the
                    # author's manifesto.txt with a copy of itself at best, or with a stale
                    # AI-era snapshot from a previous run at worst.
                    msnap = meta.get("manifesto_snapshot", "")
                    if MANIFESTO_ENABLED and not MANIFESTO_AUTHORED and msnap and os.path.exists(msnap):
                        try:
                            shutil.copyfile(msnap, MANIFESTO_FILE)
                            print(f"[run] Restored manifesto snapshot from {msnap}.", flush=True)
                        except Exception as e:
                            print(f"[run] Could not restore manifesto snapshot: {e}", flush=True)
                    # Without this, _manifesto_turn defaults to 0 on the fresh process and the
                    # very next checkpoint (turn > 0) fires an immediate, spurious revision even
                    # though the manifesto is already current as of the last checkpoint.
                    _manifesto_turn = meta.get("manifesto_turn", _last_checkpoint_turn)
                    if _manifesto_turn < 0:
                        _manifesto_turn = 0

                    _open_log_files(jp, tp, meta["run_id"])
                    _write_txt(
                        f"\n{'─' * 60}\n"
                        f"[SERVER RESTARTED — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n"
                        f"{'─' * 60}\n"
                    )
                    _resuming = True
                    print(f"[run] Continuing run {RUN_ID}", flush=True)
                    return

                print("[run] Starting fresh run", flush=True)
    except Exception as e:
        print(f"[run] Could not load previous run ({e}) — starting fresh", flush=True)
    _start_new_run()

# ── Memory helpers ────────────────────────────────────────────────────────────

def _load_memory():
    global _memory_lines, _memory_saved_mtime
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, encoding="utf-8") as f:
                _memory_lines = [l.rstrip("\n") for l in f if l.strip()]
            _memory_saved_mtime = os.path.getmtime(MEMORY_FILE)
            print(f"[memory] Loaded {len(_memory_lines)} entries from {MEMORY_FILE}", flush=True)
        except Exception as e:
            print(f"[memory] Could not load {MEMORY_FILE}: {e}", flush=True)


def _save_memory():
    global _memory_saved_mtime
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(_memory_lines) + "\n")
        _memory_saved_mtime = os.path.getmtime(MEMORY_FILE)
    except Exception as e:
        print(f"[memory] Write failed: {e}", flush=True)


def _reload_memory_if_edited():
    """If memory.txt has a different mtime than the server's last write, a human hand-edited the
    live file — reload it so the edit isn't clobbered by the in-RAM list. Caller must hold
    _memory_lock. Returns True if it reloaded."""
    global _memory_lines, _memory_saved_mtime
    if not os.path.exists(MEMORY_FILE):
        return False
    try:
        if os.path.getmtime(MEMORY_FILE) == _memory_saved_mtime:
            return False
        with open(MEMORY_FILE, encoding="utf-8") as f:
            _memory_lines = [l.rstrip("\n") for l in f if l.strip()]
        _memory_saved_mtime = os.path.getmtime(MEMORY_FILE)
        print(f"[memory] Picked up hand-edited {MEMORY_FILE} ({len(_memory_lines)} entries).", flush=True)
        return True
    except Exception as e:
        print(f"[memory] Could not reload {MEMORY_FILE}: {e}", flush=True)
        return False


def _archive_memory(lines: list[str], reason: str) -> bool:
    """Append entries to the archive file (never truncated) before they leave live memory,
    so a human can always recover them. Returns True only if the write succeeded."""
    if not lines:
        return True
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(MEMORY_ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(f"# ── {stamp} — {reason} ──\n")
            f.write("\n".join(lines) + "\n")
        return True
    except Exception as e:
        print(f"[memory] Archive write failed: {e}", flush=True)
        return False


# ── Manifesto helpers ─────────────────────────────────────────────────────────

def _load_manifesto():
    global _manifesto, _manifesto_mtime
    if not MANIFESTO_ENABLED:
        return
    if os.path.exists(MANIFESTO_FILE):
        try:
            with open(MANIFESTO_FILE, encoding="utf-8") as f:
                _manifesto = f.read().strip()
            _manifesto_mtime = os.path.getmtime(MANIFESTO_FILE)
            if _manifesto:
                label = "human-written " if MANIFESTO_AUTHORED else ""
                print(f"[manifesto] Loaded {len(_manifesto)} {label}chars from {MANIFESTO_FILE}", flush=True)
        except Exception as e:
            print(f"[manifesto] Could not load {MANIFESTO_FILE}: {e}", flush=True)
    if MANIFESTO_AUTHORED:
        if _manifesto:
            # Log the standing plan so the export has a manifesto entry to show: in "human"
            # mode nothing else ever writes one.
            log_manifesto(_manifesto_turn, "human", _manifesto)
        else:
            print(f"[manifesto] mode=human but {MANIFESTO_FILE} is empty — no manifesto will be "
                  f"injected until you write one.", flush=True)


def _reload_authored_manifesto(turn: int):
    """Re-read a human-written manifesto if the file changed on disk.

    Lets the author revise the standing plan mid-run without restarting the server. Called at
    checkpoints only — a decision must never see the manifesto change out from under the prompt
    it was built with. A changed text is logged as a new version so the export shows the history.
    """
    global _manifesto, _manifesto_mtime
    if not MANIFESTO_AUTHORED or not os.path.exists(MANIFESTO_FILE):
        return
    try:
        mtime = os.path.getmtime(MANIFESTO_FILE)
        if mtime == _manifesto_mtime:
            return
        with open(MANIFESTO_FILE, encoding="utf-8") as f:
            text = f.read().strip()
        _manifesto_mtime = mtime
        with _manifesto_lock:
            if text == _manifesto:
                return
            _manifesto = text
        log_manifesto(turn, "human", text)
        _log_activity("manifesto", desc=f"Reloaded human-written manifesto (Turn {turn})")
        print(f"[manifesto] Reloaded human-written manifesto at Turn {turn}.", flush=True)
    except Exception as e:
        print(f"[manifesto] Reload failed: {e}", flush=True)


def _save_manifesto():
    # In "human" mode manifesto.txt belongs to the author, not the server — never write it.
    if not MANIFESTO_ENABLED or MANIFESTO_AUTHORED:
        return
    try:
        with open(MANIFESTO_FILE, "w", encoding="utf-8") as f:
            f.write(_manifesto)
    except Exception as e:
        print(f"[manifesto] Write failed: {e}", flush=True)


def _snapshot_manifesto():
    """Copy the live manifesto to the per-run snapshot used for resume restoration."""
    # Nothing to snapshot in "human" mode: the manifesto never changes behind the author's
    # back, so manifesto.txt itself is the source of truth across restarts and resumes.
    if not MANIFESTO_ENABLED or MANIFESTO_AUTHORED or not _manifesto_snapshot_path:
        return
    try:
        with _manifesto_lock:
            text = _manifesto
        with open(_manifesto_snapshot_path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        print(f"[manifesto] Snapshot failed: {e}", flush=True)


# The four fixed sections every manifesto (draft and revision) must contain.
MANIFESTO_SECTIONS = (
    "Structure it as exactly these four sections, in this order, each a short paragraph "
    "(2-3 sentences). Lead each section with its heading in bold:\n"
    "1. **Economy** — your stance on planned versus market direction, and the single economic "
    "priority you will spend political capital on.\n"
    "2. **Immigration** — your policy direction (relaxed or restrictive) and the reasoning.\n"
    "3. **Term Focus** — the one or two defining goals that will define your term and win re-election (healthcare, law, military, or education).\n"
    "4. **Foreign Alignment** — East (the CSP), West (the ATO), or Neutral, and why.\n"
)

_MANIFESTO_SYS = ("You are Anton Rayne writing a private strategic plan for your presidential term.")


def _draft_manifesto(context: list[str], turn: int, stats: str = ""):
    """Draft the initial Presidential Manifesto at the prologue→turn-1 boundary.

    Runs synchronously on the first phase=main decision so the manifesto is present for
    that very decision. Guarded by _manifesto_busy / non-empty check by the caller.
    """
    global _manifesto, _manifesto_turn
    ctx = "\n".join(context[-60:]) if context else "(The prologue has just concluded.)"
    prompt = (
        "You are Anton Rayne, about to begin your first presidential term. Before the work "
        "begins, set down your Presidential Manifesto: the priorities and strategy you will "
        "pursue to improve the country and win re-election.\n\n"
        f"What you have experienced so far:\n{ctx}\n\n"
        + (f"Current state of the country:\n{stats}\n\n" if stats else "")
        + MANIFESTO_SECTIONS
        + "No preamble; just the four headed sections."
    )
    try:
        with _llm_lock:
            resp = llm.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _MANIFESTO_SYS},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=MANIFESTO_MAX_TOKENS,
                temperature=TEMPERATURE,
            )
        text = strip_think_tags(resp.choices[0].message.content or "").strip()
        if text:
            with _manifesto_lock:
                _manifesto = text
                _manifesto_turn = turn
            _save_manifesto()
            _snapshot_manifesto()
            log_manifesto(turn, "draft", text)
            _log_activity("manifesto", desc=f"Drafted Presidential Manifesto (Turn {turn})")
            print(f"[manifesto] Drafted at Turn {turn}.", flush=True)
    except Exception as e:
        print(f"[manifesto] Draft failed: {e}", flush=True)


def _revise_manifesto(context: list[str], turn: int, stats: str = ""):
    """Revise the manifesto for the rest of the term. Runs in the checkpoint worker thread."""
    global _manifesto
    with _manifesto_lock:
        current = _manifesto
    if not current:
        return
    ctx = "\n".join(context[-80:]) if context else ""
    prompt = (
        f"Your current Presidential Manifesto:\n{current}\n\n"
        f"Events since you last reviewed it (now Turn {turn}):\n{ctx or '(no notable new dialogue)'}\n\n"
        + (f"Current state of the country:\n{stats}\n\n" if stats else "")
        + "Based on the events so far, you may revise your Presidential Manifesto to continue improving the country. Keep the same "
        "four sections. If you abandon "
        "a prior position, add one short clause on why.\n\n"
        + MANIFESTO_SECTIONS
        + "\nNo preamble; just the four headed sections."
    )
    try:
        with _llm_lock:
            resp = llm.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _MANIFESTO_SYS},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=MANIFESTO_MAX_TOKENS,
                temperature=TEMPERATURE,
            )
        text = strip_think_tags(resp.choices[0].message.content or "").strip()
        if text:
            with _manifesto_lock:
                _manifesto = text
            _save_manifesto()
            log_manifesto(turn, "revision", text)
            _log_activity("manifesto", desc=f"Revised manifesto (Turn {turn})")
            print(f"[manifesto] Revised at Turn {turn}.", flush=True)
    except Exception as e:
        print(f"[manifesto] Revision failed: {e}", flush=True)


def _memory_tokens(lines: list[str]) -> int:
    return sum(est_tokens(l) for l in lines)


def _append_memory(entry: str):
    global _compacting
    with _memory_lock:
        # Pick up any hand-edit to memory.txt before appending, so the in-RAM list doesn't
        # overwrite it (the server, not RAM, is stale after an external edit).
        _reload_memory_if_edited()
        _memory_lines.append(entry)
        # Hard FIFO backstop in case compaction can't run (e.g. the LLM is unreachable).
        if len(_memory_lines) > MEMORY_HARD_LINE_CAP:
            del _memory_lines[:-MEMORY_HARD_LINE_CAP]
        _save_memory()
        should_compact = (not _compacting and
                          _memory_tokens(_memory_lines) >= MEMORY_TOKEN_CEILING)
        if should_compact:
            _compacting = True
    print(f"[memory] {entry}", flush=True)
    if should_compact:
        threading.Thread(target=_run_compaction, daemon=True).start()


def _run_compaction():
    """Summarize the oldest memory entries to free space, preserving recent ones verbatim.

    Token-budgeted: keep the most recent MEMORY_KEEP_RECENT_TOKENS verbatim and compress
    everything older into a handful of bullets, so memory settles back below the ceiling."""
    global _compacting
    try:
        with _memory_lock:
            n = len(_memory_lines)
            if _memory_tokens(_memory_lines) < MEMORY_TOKEN_CEILING:
                return
            # Walk back from the newest, reserving the keep-recent token budget; everything
            # before that split index is compacted.
            kept, split = 0, n
            for i in range(n - 1, -1, -1):
                kept += est_tokens(_memory_lines[i])
                if kept >= MEMORY_KEEP_RECENT_TOKENS:
                    split = i
                    break
            to_compact = list(_memory_lines[:split])
        if not to_compact:
            return

        print(f"[memory] Compacting {len(to_compact)} old entries…", flush=True)
        text    = "\n".join(to_compact)
        prompt  = (
            f"These are entries from Anton Rayne's private journal:\n{text}\n\n"
            "Compress them into 5–8 concise bullets that preserve his key thoughts. "
            "One sentence each. Start every bullet with '•'."
        )
        kwargs = dict(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You compress a political journal, preserving the writer's subjective judgments, alliances, and strategy. Be specific."},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=8192,
            temperature=SUMMARY_TEMPERATURE,
        )
        if ENABLE_THINKING:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": SUMMARY_ENABLE_THINKING}}

        # Retry a few times: the LLM may error transiently or return nothing usable.
        summary = []
        for attempt in range(1, MEMORY_COMPACT_ATTEMPTS + 1):
            try:
                with _llm_lock:
                    response = llm.chat.completions.create(**kwargs)
                raw      = strip_think_tags(response.choices[0].message.content or "").strip()
                bullets  = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                summary  = [f"[Summary] {b.lstrip('•').strip()}" for b in bullets if b]
                if summary:
                    break
                print(f"[memory] Compaction attempt {attempt}/{MEMORY_COMPACT_ATTEMPTS} returned nothing.", flush=True)
            except Exception as e:
                print(f"[memory] Compaction attempt {attempt}/{MEMORY_COMPACT_ATTEMPTS} failed: {e}", flush=True)

        archived = False
        if not summary:
            # LLM gave nothing after every attempt — don't compact. Move the oldest entries
            # to the archive file (never dropped, so a human can recover them later) before
            # they leave live memory. If even the archive write fails, bail without touching
            # live memory — the hard FIFO cap remains the last-resort backstop.
            archived = _archive_memory(to_compact, f"compaction unavailable after {MEMORY_COMPACT_ATTEMPTS} attempts")
            if not archived:
                print("[memory] Archive failed — leaving memory intact for a later retry.", flush=True)
                return

        with _memory_lock:
            # Keep entries added while the LLM was running (indices ≥ n)
            added_since = list(_memory_lines[n:])
            recent      = list(_memory_lines[split:n])
            _memory_lines.clear()
            if summary:
                # Successful compaction: replace the old block with its summary.
                _memory_lines.extend(summary)
            else:
                # Entries are safe in the archive; leave a marker pointing there so a human
                # can see the summary is missing and intervene.
                _memory_lines.append(f"[Archived] {len(to_compact)} older entries moved to {MEMORY_ARCHIVE_FILE} — compaction unavailable.")
            _memory_lines.extend(recent)
            _memory_lines.extend(added_since)
            _save_memory()

        if summary:
            print(f"[memory] Compacted {len(to_compact)} → {len(summary)} lines", flush=True)
            _log_activity("compact", desc=f"Compacted {len(to_compact)} → {len(summary)} entries")
        else:
            print(f"[memory] Compaction unavailable after {MEMORY_COMPACT_ATTEMPTS} attempts — archived {len(to_compact)} oldest entries to {MEMORY_ARCHIVE_FILE}.", flush=True)
            _log_activity("compact", desc=f"Compaction unavailable — archived {len(to_compact)} oldest entries")
    except Exception as e:
        print(f"[memory] Compaction failed: {e}", flush=True)
    finally:
        _compacting = False


def _clear_memory():
    with _memory_lock:
        _memory_lines.clear()
        _save_memory()
    print("[memory] Cleared", flush=True)


def _first_sentence(text: str, max_chars: int = 500) -> str:
    """Clip a journal entry to its first sentence. Reasoning models ignore 'one sentence' and
    dump multi-paragraph analyses (headings, bullets); collapsing whitespace folds those onto
    one line, then we keep up to the first sentence terminator (with a hard char backstop)."""
    text = " ".join(text.split())
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    s = (m.group(1) if m else text).strip()
    if len(s) > max_chars:
        s = s[:max_chars].rsplit(" ", 1)[0] + "…"
    return s


def _generate_memory_entry(context: list[str], turn: int, fragment: str):
    """Background thread: ask the LLM for Anton's subjective, first-person read on the scene
    just played — one sentence (clipped by _first_sentence regardless of what the model returns).
    Facts live in the ledger now, so memory is judgment only — the terse per-decision factual
    entries were dropped as redundant with the ledger. No-op if the fragment had no actual choice.
    """
    if not context or not any(l.startswith("[CHOICE]: ") for l in context):
        return
    # Skip if the fragment was only a DecisionPanel event label / bill sign-veto + choice (no
    # real conversation).
    if not any(not l.startswith(("[CHOICE]: ", "[AUTO]: ", "Event: ", "Bill for decision: "))
               for l in context):
        return
    context_text = "\n".join(context)
    prompt = (
        f"This is a transcript of a political scene:\n{context_text}\n\n"
        "Write a brief factual summary (1-3 sentences) of what happened and what decisions were made.\n"
        "Do NOT use headings, bullet points, lists, sections, or "
        "any preamble.\n"
    )
    sys_prompt = ("You write factual log entries based on transcripts of events. Report only what happened. "
                  "Never use lists, headings, or sections.")

    try:
        kwargs = dict(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=8192,
            temperature=SUMMARY_TEMPERATURE,
        )
        if ENABLE_THINKING:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": SUMMARY_ENABLE_THINKING}}

        with _llm_lock:
            response = llm.chat.completions.create(**kwargs)
        raw = strip_think_tags(response.choices[0].message.content or "").strip().strip('"')
        # Hard brevity guard: a verbose model still gets clipped to one sentence so the journal
        # can't bloat regardless of how the model ignores the instruction.
        #raw = _first_sentence(raw)
        if raw:
            _append_memory(f"[Turn {turn}] {raw}")
            _log_activity("memory", desc=f"[Turn {turn}] {raw[:80]}")
    except Exception as e:
        print(f"[memory] Generation failed: {e}", flush=True)


def _effective_system_prompt(phase: str) -> str:
    base = PROLOGUE_SYSTEM_PROMPT if phase == "prologue" else SYSTEM_PROMPT
    parts = [base]
    # Manifesto sits between the persona/rules and memory (volatility ordering). Never in the
    # prologue — it doesn't exist yet and the prologue is pre-presidency.
    if MANIFESTO_ENABLED and phase != "prologue":
        with _manifesto_lock:
            man = _manifesto
        if man:
            parts.append(f"## Presidential Manifesto\n{man}")
    with _memory_lock:
        if _memory_lines:
            parts.append("## Your notes so far\n" + "\n".join(_memory_lines))
    # Ledger last in the system block: the permanent factual spine (durable facts the game itself
    # records), distinct from memory's subjective judgment. Never in the prologue (pre-presidency).
    if phase != "prologue" and _last_journal:
        parts.append("## Official record of decisions\n" + _last_journal)
    return "\n\n".join(parts)


def _snapshot_memory():
    """Copy the live memory log to the per-run snapshot used for resume restoration."""
    if not _memory_snapshot_path:
        return
    try:
        with _memory_lock:
            lines = list(_memory_lines)
        with open(_memory_snapshot_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
    except Exception as e:
        print(f"[memory] Snapshot failed: {e}", flush=True)


def _checkpoint_memory(ctx_snapshot: list[str], turn: int, fragment: str,
                       revise_manifesto: bool = False, stats: str = ""):
    """Checkpoint worker: generate the fragment summary, optionally revise the manifesto,
    then snapshot both.

    Snapshotting *after* generation/revision captures the just-finished fragment's summary
    and the up-to-date manifesto, so a resume from this checkpoint keeps both aligned with
    the committed game state.
    """
    _generate_memory_entry(ctx_snapshot, turn, fragment)
    if revise_manifesto:
        _revise_manifesto(ctx_snapshot, turn, stats)
    _snapshot_memory()
    _snapshot_manifesto()

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
            # Multi-select pages commit a set; `index` is only its first element, so the panel
            # needs the whole thing to mark every chosen option. `is None` keeps a deliberate
            # empty set empty — see the TXT render below.
            "choice_indices": (entry.get("choice_indices")
                               if entry.get("choice_indices") is not None else [index]),
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
    with _json_lock:
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

    # Multi-select decisions mark every chosen option; single-select falls back to [index].
    # `is None` rather than `or`: an empty set is a real answer ("select nothing"), and treating
    # it as missing would fall back to [index] — which is 0 for an empty set — and print a → next
    # to an option the model never picked.
    sel = entry.get("choice_indices")
    if sel is None:
        sel = [index]
    arrow_choices = "\n".join(
        f"  {'→' if c['index'] in sel else ' '} {c['index']}. {c['text']}"
        for c in choices
    )
    chosen_text = ", ".join(choices[i]["text"] for i in sel if i < len(choices)) or "(none selected)"
    _write_txt(f"\n{arrow_choices}\n")
    _write_txt(f"\n[{DISPLAY_NAME}] → {chosen_text}\n")
    if reasoning:
        _write_txt(f'  "{reasoning}"\n')


def log_checkpoint(entry: dict):
    with _json_lock:
        _json_log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _json_log.flush()
    turn, step = entry.get("turn", 0), entry.get("step", 0)
    _write_txt(f"\n{'─' * 60}\n[AUTOSAVE — Turn {turn}, Step {step}]\n{'─' * 60}\n")


def log_manifesto(turn: int, kind: str, text: str):
    """Append a manifesto version to the JSONL so the export can show its evolution.

    `kind` is "draft" or "revision". Written under _json_lock since this is called from the
    decision thread (draft) and the checkpoint worker thread (revision).
    """
    global _manifesto_version
    _manifesto_version += 1
    entry = {
        "type":      "manifesto",
        "run_id":    RUN_ID,
        "timestamp": datetime.now().isoformat(),
        "turn":      turn,
        "version":   _manifesto_version,
        "kind":      kind,
        "text":      text,
    }
    with _json_lock:
        _json_log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _json_log.flush()


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
    man_tok = 0
    jrn_tok = 0
    if phase != "prologue":
        with _manifesto_lock:
            man_tok = est_tokens(_manifesto)
        jrn_tok = est_tokens(_last_journal)
    budget = CONTEXT_WINDOW - MAX_TOKENS - est_tokens(base_sys) - mem_tok - man_tok - jrn_tok - PROMPT_OVERHEAD_TOKENS
    if MAX_CONTEXT_TOKENS is not None:
        budget = min(budget, MAX_CONTEXT_TOKENS)
    return max(0, budget)


def trim_context(context: list[str], budget: int) -> list[str]:
    """Drop oldest lines until the context fits within budget tokens."""
    lines = list(context)
    while lines and sum(est_tokens(l) for l in lines) > budget:
        lines.pop(0)
    return lines


def _restore_resume_context():
    """Seed the rolling context from the truncated JSONL when continuing a run.

    Runs after memory is loaded so context_budget() reflects the real memory cost. The
    server-side window (_prev_context) and the panel transcript are populated; the C#
    plugin seeds its own _context from GET /resume.

    _fragment_context is deliberately NOT seeded, though it is fed from the same diff during
    normal play. It is the span the next memory entry summarises, and everything restored here
    has already been summarised: memory.txt was just restored from the snapshot taken at this
    very checkpoint. Seeding it made the first post-resume checkpoint summarise the whole
    restored window — many turns of committed history — into one entry stamped with the current
    turn, duplicating memory the run already had and reading as if the turn contained all of it.
    The replay starts from the autosave ≡ this checkpoint, so the next fragment starts empty.
    """
    if not _resuming:
        return
    restored = _restore_context_from_jsonl(_jsonl_path)
    if restored:
        _prev_context.extend(restored)
        _panel_append(restored)
    print(f"[run] Restored {len(restored)} context lines (through last checkpoint).", flush=True)


# ── Initialization (order matters: run → memory → context) ────────────────────
# Run selection truncates the JSONL and restores the memory snapshot; memory must load
# before the context restore so context_budget() subtracts the real memory cost.
_load_or_create_run()
_load_memory()
_load_manifesto()
_restore_resume_context()


def _news_window(full: str, turn: int):
    """Pick the rotating window of articles the AI sees this decision.

    Mirrors the cursor that used to live in C# GameStateReader: march forward through the
    current turn's article list a few at a time so successive decisions surface fresh papers
    and the whole set is covered across the turn; reset when the turn changes. Returns
    (sent_text, all_lines, sent_lines)."""
    global _news_turn, _news_offset
    lines = [l for l in full.split("\n") if l.strip()]
    n = len(lines)
    if n == 0:
        return "", [], []
    if turn != _news_turn:
        _news_turn   = turn
        _news_offset = 0
    _news_offset %= n
    take = min(ARTICLES_PER_READ, n)
    idxs = [(_news_offset + k) % n for k in range(take)]
    _news_offset = (_news_offset + take) % n
    sent = [lines[i] for i in idxs]
    return "\n".join(sent), lines, sent


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
                 news: str = "", reports: str = "", economy: str = "",
                 min_select: int = 0, max_select: int = 1, question: str = "",
                 turn: int = 0) -> str:
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
    # reasoning is written after the index (see _response_format), so it is stated as an
    # explanation of a choice already made — a bare length hint let the model fill it with
    # pre-decision deliberation instead.
    reasoning_hint = ("2-3 sentences explaining the choice you just made"
                      if major else "one sentence explaining the choice you just made")
    thinking_hint  = "consider the options, then decide"

    # The thinking scratchpad is only for models that lack a native one. A reasoning model already
    # deliberates in <think> (stripped by strip_think_tags), so asking for the field as well buys
    # nothing and charges twice for the same work. Both paths keep the same deliberate → commit →
    # justify shape; only the deliberation venue differs. Note this example is the *only* shape
    # guidance on the thinking path — _response_format sends no schema there.
    think_field = "" if ENABLE_THINKING else f'"thinking": "{thinking_hint}", '

    # Inject codex entries most-referenced since the last checkpoint, scaled to how many
    # unique entries have been seen in this fragment (more references → more injection).
    codex_block = ""
    global _last_codex_injected, _last_codex_tokens, _last_codex_debug
    _last_codex_tokens = 0
    if codex_refs and _fragment_codex_refs:
        unique_count  = len(_fragment_codex_refs)
        max_inject    = min(unique_count, 7)
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
                {"title": _codex[r].get("title", r), "summary": _codex[r]["summary"],
                 "raw": _codex[r].get("raw", "")}
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

    # User-block order (background → decision): the volatile background (press/reports/codex)
    # leads, then the state read-out (stats + economy trajectory) sits above the dialogue so the
    # numbers frame the conversation, and the dialogue itself sits closest to the decision.
    sections = []
    if news:             sections.append(f"Press:\n{news.strip()}")
    if selected_reports: sections.append(f"Reports:\n{selected_reports.strip()}")
    if codex_block:      sections.append(f"Relevant context:\n{codex_block}")
    if stats:            sections.append(f"Current stats: {stats}")
    if economy:          sections.append(f"Economic trajectory (recent → now):\n{economy.strip()}")
    sections.append(f"Recent dialogue:\n{context_text}")

    # Restate the key figures in the instruction tail (the strongest attention slot, right before
    # the JSON format line) so the budget/approval numbers are reliably weighed in the choice.
    #key_figures = f"Weigh these as you choose: {stats.strip()}\n\n" if stats else ""

    # Multi-select page (e.g. emergency decrees): the model returns a set of indices within the
    # page's [min, max] choice bounds. Single-select pages keep the single choice_index format.
    #
    # Key order matters and must match the declaration order in _response_format(): under schema
    # enforcement the grammar emits properties in that order, so an example in any other order
    # primes a shape the model is then forbidden from producing — it plans its answer around the
    # example and gets masked into the schema's order token by token.
    note = ""
    if max_select > 1:
        if min_select >= max_select:
            count_phrase = f"exactly {max_select}"
        elif min_select <= 0:
            count_phrase = f"up to {max_select}"
        else:
            count_phrase = f"between {min_select} and {max_select}"
        instruction = f"Select {count_phrase} of the following options, choosing the best combination"
        # "up to N" permits zero but only states a ceiling, and a model reading it as a quota
        # picks something rather than nothing. Where the page's minimum really is zero, abstaining
        # is a legal answer worth saying outright — along with the exact token that expresses it,
        # since an empty list is easy to intend and impossible to guess. Sits after the options
        # and against the format line: it is a statement about how to answer, not a fourth option.
        if min_select <= 0:
            note = ('Selecting none of them is also valid. If no option should be enacted, '
                    'return an empty list: "choice_indices": []\n\n')
        fmt = (f'Respond with JSON only: {{{think_field}'
               f'"choice_indices": [N, ...], "reasoning": "{reasoning_hint}"}}')
    else:
        if question:
            instruction = "Select exactly one option"
        fmt = (f'Respond with JSON only: {{{think_field}'
               f'"choice_index": N, "reasoning": "{reasoning_hint}"}}')

    # The live question is rendered here rather than appended to the rolling context. A paged panel
    # asks a run of questions, and turn 1 opens several such panels back to back, so the context
    # tail is already a column of this panel's question/[CHOICE] pairs. A question sitting in that
    # column unanswered reads as the last field of one open multi-part form and the model fills in
    # the whole form; stated here — after the settled context, immediately above the options — it
    # reads as the single decision it is. Context lines are always settled pairs (see
    # PagedDecisionDriver.RecordSettled).
    ask = f"The decision in front of you now:\n{question.strip()}\n\n" if question else ""

    return (
        f"Current turn: {turn} / 11\n\n"
        + "\n\n".join(sections) + "\n\n"
        + ask
        + f"{instruction}:\n{choices_text}\n\n"
        + note
        + fmt
    )


def _response_format(multi: bool, min_select: int, max_select: int):
    """Build the per-request response_format. The config blob only selects the mode (absent =
    unconstrained, json_object, or json_schema); for json_schema the schema is generated here
    rather than read verbatim, for two reasons:

      - Multi-select pages ask for choice_indices (see build_prompt), so one fixed choice_index
        schema would constrain the model into the wrong shape on those pages.
      - Grammar-constrained decoding emits properties in declaration order, so the order here is
        load-bearing: thinking, then reasoning, then the index. build_prompt's example must be
        kept in the same order.

    Indices are deliberately left unbounded here — parse_response/parse_multi_response already
    clamp them to the choice list, and integer range grammars are the least portable part of
    llama.cpp's schema support."""
    fmt = config.get("response_format")
    if not fmt or fmt.get("type") != "json_schema":
        return fmt
    # Order: deliberate, commit, then justify.
    #
    # thinking is a scratchpad: the model works through the options there. The parsers never read
    # it, so it is discarded on arrival — the schema-side equivalent of <think> + strip_think_tags.
    # It exists because a non-reasoning model has nowhere else to work.
    #
    # reasoning comes *after* the index on purpose. Written before it, the model has not committed
    # to anything yet and honestly reports the act of deciding ("I need to weigh X against Y")
    # rather than the decision — reasoning that reads as desynced from the choice shown next to it.
    # After the index, the choice is already on the page and reasoning can only explain it.
    thinking = {"type": "string"}
    reasoning = {"type": "string"}
    if multi:
        indices = {"type": "array", "items": {"type": "integer"}, "maxItems": max_select}
        if min_select > 0:
            indices["minItems"] = min_select
        name  = "multi_decision"
        props = {"thinking": thinking, "choice_indices": indices, "reasoning": reasoning}
    else:
        name  = "decision"
        props = {"thinking": thinking, "choice_index": {"type": "integer"}, "reasoning": reasoning}
    return {
        "type": "json_schema",
        "json_schema": {
            "name":   name,
            "strict": True,
            "schema": {
                "type":                 "object",
                "properties":           props,
                "required":             list(props),
                "additionalProperties": False,
            },
        },
    }


def strip_think_tags(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def strip_thinking_member(content: str) -> str:
    """Drop the "thinking" scratchpad member from a raw reply. Only used to protect the bare-digit
    fallbacks in the parsers: the scratchpad is prose and routinely contains digits (years, option
    numbers restated), which a naive digit scan would mistake for the choice."""
    return re.sub(r'"thinking"\s*:\s*"(?:[^"\\]|\\.)*"\s*,?', "", content, flags=re.DOTALL)


def _coerce_index(val) -> int:
    """Coerce a model-supplied choice_index to an int. Without schema enforcement (which is
    skipped when enable_thinking is on) the model sometimes returns a list (e.g. [2]) or a
    stringy/decimal value — handle those instead of letting int() raise a 500."""
    if isinstance(val, list):
        val = val[0] if val else 0
    if isinstance(val, bool):   # bool is an int subclass; treat as 0/1 explicitly
        return int(val)
    if isinstance(val, str):
        m = re.search(r"-?\d+", val)
        val = m.group() if m else 0
    return int(float(val))


def parse_response(content: str, num_choices: int) -> tuple[int, str, bool]:
    """Returns (index, reasoning, ok). `ok` is False when no choice could be extracted at all
    (invalid/absent JSON and no bare number) — the caller uses that to re-query the LLM. When ok
    is False the returned index is the default (0)."""
    clean = strip_think_tags(content)
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if "choice_index" in data:
                index = max(0, min(_coerce_index(data["choice_index"]), num_choices - 1))
                return index, str(data.get("reasoning", "")).strip(), True
        except (json.JSONDecodeError, ValueError, TypeError, IndexError):
            pass
    # Malformed or truncated JSON — likelier now that a thinking block precedes everything. The
    # index sits ahead of reasoning, so a max_tokens cutoff usually leaves it intact in otherwise
    # unparseable text: recover it by key before falling back to bare digits.
    keyed = re.search(r'"choice_index"\s*:\s*(-?\d+)', clean)
    if keyed:
        return max(0, min(int(keyed.group(1)), num_choices - 1)), "", True
    digit = re.search(r"\d+", strip_thinking_member(clean))
    if digit:
        index = max(0, min(int(digit.group()), num_choices - 1))
        return index, "", True
    return 0, "", False


def parse_multi_response(content: str, num_choices: int,
                         min_sel: int, max_sel: int) -> tuple[list[int], str, bool]:
    """Parse a multi-select reply ({"choice_indices": [...]}). Dedups and bounds each index,
    clamps the set to max_sel, then pads up to min_sel with the next unused options (in listed
    order) so the page always has enough boxes checked to submit.

    Returns (indices, reasoning, ok). `ok` is False when nothing usable could be extracted (no
    JSON indices and no bare numbers) — the caller uses that to re-query the LLM. The min_sel
    padding still runs on failure so the page can always be submitted as a last resort.

    An explicitly empty `choice_indices: []` is a deliberate "select nothing" — a legal answer on
    a page with min_sel == 0 — and is reported as ok. It must not reach the bare-integer fallback
    below: that scans the reply text, which still holds `reasoning` prose, so a refusal like
    "none of these warrant Article 2 powers" would have its digits mined into a selection the
    model never made. (When min_sel > 0 the padding below still fills the set.)"""
    clean = strip_think_tags(content)
    indices: list[int] = []
    reasoning = ""
    explicit_empty = False
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            raw = data.get("choice_indices", data.get("choice_index", []))
            if not isinstance(raw, list):
                raw = [raw]
            # Only an empty list the model actually sent counts — not the [] default above, which
            # would make a reply carrying neither key look like an intentional empty selection.
            explicit_empty = "choice_indices" in data and raw == []
            for v in raw:
                try:
                    iv = _coerce_index(v)
                except (ValueError, TypeError):
                    continue
                if 0 <= iv < num_choices and iv not in indices:
                    indices.append(iv)
            reasoning = str(data.get("reasoning", "")).strip()
        except (json.JSONDecodeError, ValueError, TypeError, IndexError):
            pass
    if not indices and not explicit_empty:
        # Fallback: pull any bare integers out of the raw text, minus the thinking scratchpad —
        # its prose is full of digits that would otherwise be read as selections.
        for d in re.findall(r"\d+", strip_thinking_member(clean)):
            iv = int(d)
            if 0 <= iv < num_choices and iv not in indices:
                indices.append(iv)
    ok = bool(indices) or explicit_empty
    if max_sel > 0 and len(indices) > max_sel:
        indices = indices[:max_sel]
    if min_sel > 0 and len(indices) < min_sel:
        for i in range(num_choices):
            if len(indices) >= min_sel:
                break
            if i not in indices:
                indices.append(i)
    return indices, reasoning, ok


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
    data       = request.get_json(force=True)
    request_id = data.get("request_id")

    # Fast path: the original already finished and cached its result — replay it without taking the
    # processing lock (so a completed dedup hit never blocks behind a live decision).
    cached = _decision_cache_get(request_id)
    if cached is not None:
        print(f"[dedup] replaying cached decision for request_id={request_id}", flush=True)
        return jsonify(cached)

    # Slow path: serialize so a retry that arrived while the original is still running coalesces
    # onto it instead of launching a second LLM call. Re-check the cache once we hold the lock —
    # if we were a waiting retry, the original has now finished and cached its answer.
    with _decision_proc_lock:
        cached = _decision_cache_get(request_id)
        if cached is not None:
            print(f"[dedup] coalesced retry for request_id={request_id}", flush=True)
            return jsonify(cached)
        return _decision_impl(data, request_id)


def _decision_impl(data, request_id):
    global _last_news, _last_news_full, _last_news_debug, _last_reports, _last_stats, \
        _last_journal, _last_economy, _cur_turn, _cur_step, _cur_fragment
    decision_type = data.get("type", "dialogue")
    context       = data.get("context", [])
    choices       = data.get("choices", [])
    turn          = data.get("turn", 0)
    step          = data.get("step", 0)
    fragment      = data.get("fragment", "")
    phase         = data.get("phase", "main")
    # The question the panel is asking right now, when the panel supplies one (paged decisions).
    # Kept out of `context` on purpose — see build_prompt.
    question      = data.get("question", "")
    stats      = data.get("stats", "")
    news       = data.get("news", "")
    reports    = data.get("reports", "") or _last_reports
    journal    = data.get("journal", "")
    economy    = data.get("economy", "") or _last_economy
    codex_refs = data.get("codex_refs", [])
    # Multi-select (checkbox) pages send min_select/max_select; max_select > 1 ⇒ pick a set.
    min_select = int(data.get("min_select", 0) or 0)
    max_select = int(data.get("max_select", 1) or 1)
    multi      = max_select > 1
    _cur_turn, _cur_step, _cur_fragment = turn, step, fragment
    # News: C# sends the full current-turn set. Store it whole, then advance the rotating window
    # to pick what the AI sees this decision; `news` (passed to build_prompt) is that subset.
    if news: _last_news_full = news
    news, news_all, news_sent = _news_window(_last_news_full, turn)
    _last_news        = news
    _last_news_debug  = {"all": news_all, "sent": news_sent}
    if reports: _last_reports = select_reports(reports)
    # Persist journal/economy so they survive an empty payload (prologue) and feed the panel/budget.
    # _last_journal must be set before _effective_system_prompt so the ledger block is current.
    if journal: _last_journal = journal
    if economy: _last_economy = economy
    system_prompt = _effective_system_prompt(phase)

    if not choices:
        payload = {"choice_index": 0, "reasoning": "", "model_name": DISPLAY_NAME}
        _decision_cache_put(request_id, payload)
        return jsonify(payload)

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
        payload = {"choice_index": index, "reasoning": "", "model_name": "human"}
        _decision_cache_put(request_id, payload)
        return jsonify(payload)

    # Draft the Presidential Manifesto at the prologue→turn-1 boundary: the first phase=main
    # AI decision. Synchronous so the manifesto is present for this very decision.
    # Never in "human" mode: an empty manifesto.txt means the author hasn't written one, not
    # that the AI should write one for them.
    global _manifesto_busy
    if MANIFESTO_ENABLED and not MANIFESTO_AUTHORED and phase != "prologue" \
            and not _manifesto and not _manifesto_busy:
        _manifesto_busy = True
        try:
            _draft_manifesto(context, turn, stats or _last_stats)
        finally:
            _manifesto_busy = False
        # Rebuild so the freshly drafted manifesto is included in this very decision's prompt
        # (system_prompt was computed above, before the draft).
        system_prompt = _effective_system_prompt(phase)

    prompt = build_prompt(decision_type, context, choices, stats, codex_refs, phase, news, reports, economy, min_select, max_select, question, turn)

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
        # Reasoning models emit <think> before the JSON, which a schema grammar would forbid from
        # the very first token — so thinking and schema enforcement stay mutually exclusive.
        if ENABLE_THINKING:
            kwargs["extra_body"] = {"enable_thinking": True}
        else:
            fmt = _response_format(multi, min_select, max_select)
            if fmt:
                kwargs["response_format"] = fmt

        global _last_prompt_tokens, _last_completion_tokens, _last_raw_request
        global _last_gen_seconds, _last_tok_per_sec
        _last_raw_request = dict(kwargs)

        # Re-query the LLM when its reply has no extractable choice (invalid/absent JSON, no bare
        # number). Token counts and timing below reflect the final (successful or last) attempt.
        prompt_tokens = compl_tokens = 0
        gen_seconds = 0.0
        for attempt in range(PARSE_RETRIES + 1):
            # Timer inside the lock: exclude any wait for an in-flight background summary so
            # gen_seconds is pure generation time and the tok/s gauge stays accurate.
            with _llm_lock:
                _t0 = time.perf_counter()
                response = llm.chat.completions.create(**kwargs)
                gen_seconds = time.perf_counter() - _t0
            content  = response.choices[0].message.content or ""
            usage    = response.usage
            if usage:
                prompt_tokens = usage.prompt_tokens
                compl_tokens  = usage.completion_tokens
            if multi:
                indices, reasoning, ok = parse_multi_response(content, len(choices), min_select, max_select)
                index = indices[0] if indices else 0
            else:
                index, reasoning, ok = parse_response(content, len(choices))
                indices = [index]
            if ok:
                break
            if attempt < PARSE_RETRIES:
                print(f"[AI reparse] no valid choice in reply (attempt {attempt + 1}/"
                      f"{PARSE_RETRIES + 1}); re-querying LLM", flush=True)
        if stats:
            _last_stats = stats
        _last_prompt_tokens     = prompt_tokens
        _last_completion_tokens = compl_tokens
        _last_gen_seconds       = gen_seconds
        _last_tok_per_sec       = (compl_tokens / gen_seconds) if (gen_seconds > 0 and compl_tokens) else 0.0

        tps_note = f" @ {_last_tok_per_sec:.1f} tok/s" if _last_tok_per_sec else ""
        print(f"\n[{DISPLAY_NAME}] Turn {turn} / {decision_type}  [{prompt_tokens} in / {compl_tokens} out{tps_note}]", flush=True)
        if multi:
            chosen_list = ", ".join(f"{i}: {choices[i]['text']}" for i in indices if i < len(choices))
            print(f"  → [{chosen_list}]", flush=True)
        else:
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
            "question":         question,
            "choices":          choices,
            "choice_index":     index,
            "choice_indices":   indices,
            "reasoning":        reasoning,
            "prompt_tokens":    prompt_tokens,
            "completion_tokens": compl_tokens,
        }
        log_decision(entry, context, choices, index, reasoning)
        # `indices` is the full committed set (just [index] for single-select), so a multi-select
        # page reports every option it checked rather than only the first.
        chosen_desc = (", ".join(choices[i]["text"] for i in indices if i < len(choices))
                       or "(none selected)")
        _log_activity("decision", desc=f"Turn {turn} · {decision_type} → {chosen_desc[:60]}")

        # No per-decision memory write: facts now live in the ledger (journal), and Anton's
        # subjective read is generated once per fragment at the checkpoint from the full
        # conversation. Keeps memory judgment-only and low-churn.

        payload = {
            "choice_index":      index,
            "choice_indices":    indices,
            "reasoning":         reasoning,
            "model_name":        DISPLAY_NAME,
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": compl_tokens,
        }
        _decision_cache_put(request_id, payload)
        return jsonify(payload)

    except Exception as exc:
        # Do NOT cache errors — a real server-side failure should let the client's retry actually
        # re-attempt rather than replay the failure.
        print(f"[AI error] {exc}", flush=True)
        return jsonify({"error": str(exc)}), 500


@app.post("/stats")
def post_stats():
    global _last_stats, _last_news_full, _last_reports
    data = request.get_json(force=True)
    stats   = data.get("stats",   "")
    news    = data.get("news",    "")
    reports = data.get("reports", "")
    if stats:   _last_stats   = stats
    # Keep the full article set fresh for the panel; the window only advances on a real decision.
    if news:    _last_news_full = news
    if reports: _last_reports = select_reports(reports)
    return jsonify({"ok": True})


@app.post("/checkpoint")
def checkpoint():
    global _prev_context, _last_checkpoint_turn, _last_checkpoint_offset, _manifesto_turn, \
        _journal_logged_turn

    data          = request.get_json(force=True)
    turn          = data.get("turn", 0)
    step          = data.get("step", 0)
    fragment      = data.get("fragment", "")
    context       = data.get("context", [])

    _last_checkpoint_turn = turn

    # FIX: Use the sequence overlap diff
    new_lines = _get_new_lines(_prev_context, context)
    
    _fragment_context.extend(new_lines)
    if new_lines:
        with _json_lock:
            for line in new_lines:
                _json_log.write(json.dumps(
                    {"type": "dialogue", "run_id": RUN_ID, "turn": turn, "text": line},
                    ensure_ascii=False,
                ) + "\n")
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
        # State snapshot at the checkpoint boundary — the latest stats/economy known to the
        # server. Short, so kept per-checkpoint for a fine-grained time series (roadmap #10).
        "stats":     _last_stats,
        "economy":   _last_economy,
    }
    # The ledger is cumulative and long — write it at most once per turn (the first checkpoint of
    # a new turn) rather than on every checkpoint, so the JSONL doesn't balloon. The export reads
    # the most recent journal snapshot at or before each turn.
    if _last_journal and turn != _journal_logged_turn:
        entry["journal"]     = _last_journal
        _journal_logged_turn = turn
    log_checkpoint(entry)
    # The checkpoint line is now flushed to disk; record the byte size through it as the
    # resume truncation point (the autosave/JSONL transaction boundary).
    _last_checkpoint_offset = os.path.getsize(_jsonl_path)
    _save_run_meta()
    print(f"[checkpoint] Turn {turn} / Step {step} — {fragment}", flush=True)
    _fragment_codex_refs.clear()
    _log_activity("checkpoint", desc=f"Turn {turn} Step {step} · {fragment}")

    ctx_snapshot = list(_fragment_context)
    _fragment_context.clear()

    # Revise the manifesto once per turn (major-turn cadence): only when the turn has advanced
    # past the last draft/revision and a manifesto exists. The checkpoint worker does it after
    # the memory summary so the snapshot captures both. In "human" mode the AI never revises —
    # the checkpoint is instead where an edited manifesto.txt is picked up.
    if MANIFESTO_AUTHORED:
        _reload_authored_manifesto(turn)
        revise = False
    else:
        revise = MANIFESTO_ENABLED and bool(_manifesto) and turn > _manifesto_turn
        if revise:
            _manifesto_turn = turn

    # Generate the fragment memory (+ optional manifesto revision), then snapshot for resume.
    threading.Thread(
        target=_checkpoint_memory,
        args=(ctx_snapshot, turn, fragment, revise, _last_stats),
        daemon=True,
    ).start()

    return jsonify({"status": "ok"})


_SERVER_START = datetime.now()


@app.get("/health")
def health():
    uptime = (datetime.now() - _SERVER_START).total_seconds()
    return jsonify({
        "status":         "ok",
        "run_id":         RUN_ID,
        "model":          DISPLAY_NAME,
        "started_at":     _SERVER_START.isoformat(timespec="seconds"),
        "uptime_seconds": int(uptime),
        "viewers":        _viewer_count(),
    })


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


@app.get("/resume")
def resume():
    """Return the restored rolling window so the plugin can seed its own _context after a
    restart. Empty list for a fresh run — the plugin then builds context normally."""
    return jsonify({"context": list(_prev_context)})


@app.get("/manifesto")
def get_manifesto():
    with _manifesto_lock:
        text = _manifesto
    return jsonify({"manifesto": text, "turn": _manifesto_turn,
                    "enabled": MANIFESTO_ENABLED, "mode": MANIFESTO_MODE})


@app.delete("/manifesto")
def delete_manifesto():
    global _manifesto, _manifesto_turn
    # Clearing is an AI-mode affordance (wipe the plan so it gets redrafted). In "human" mode
    # there is nothing to redraft and the only copy lives in the author's file — refuse.
    if MANIFESTO_AUTHORED:
        return jsonify({"ok": False, "error": "manifesto_mode is 'human' — edit manifesto.txt instead"}), 409
    with _manifesto_lock:
        _manifesto = ""
        _manifesto_turn = 0
    _save_manifesto()
    print("[manifesto] Cleared", flush=True)
    return jsonify({"ok": True})


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
    with _manifesto_lock:
        est_manifesto_tokens = est_tokens(_manifesto)
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
        "last_gen_seconds":       round(_last_gen_seconds, 2),
        "last_tok_per_sec":       round(_last_tok_per_sec, 1),
        "max_tokens":             MAX_TOKENS,
        "context_window":         CONTEXT_WINDOW,
        "context_budget":         context_budget("main"),
        "est_context_tokens":     est_context_tokens,
        "est_memory_tokens":      est_memory_tokens,
        "est_manifesto_tokens":   est_manifesto_tokens,
        "est_sysprompt_tokens":   est_tokens(SYSTEM_PROMPT),
        "est_codex_tokens":       _last_codex_tokens,
        "est_news_tokens":        est_tokens(_last_news)    if _last_news    else 0,
        "est_reports_tokens":     est_tokens(_last_reports) if _last_reports else 0,
        "est_journal_tokens":     est_tokens(_last_journal) if _last_journal else 0,
        "est_economy_tokens":     est_tokens(_last_economy) if _last_economy else 0,
        "est_overhead_tokens":    PROMPT_OVERHEAD_TOKENS,
        "stats":                  _last_stats,
        "news":                   _last_news,
        "news_full":              _last_news_full,
        "news_sent":              _last_news_debug.get("sent", []),
        "reports":                _last_reports,
        "reports_full":           "\n".join(_last_reports_debug.get("all", [])),
        "reports_sent":           _last_reports_debug.get("selected", []),
        "journal":                _last_journal,
        "economy":                _last_economy,
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




# ── Remote read-only view ─────────────────────────────────────────────────────
# A request that arrived through the Cloudflare tunnel carries Cloudflare-injected
# headers that a direct localhost/LAN connection never has. That traffic is the remote
# viewer and is restricted to reads; the game and a local browser reach the server
# directly (no such headers) and keep full control. Fail-closed: anything that looks
# like it came from the internet is treated as read-only.
_CLOUDFLARE_MARKERS = ("Cf-Ray", "Cf-Connecting-Ip", "Cdn-Loop", "Cf-Access-Jwt-Assertion")

# GET endpoints a remote viewer may read. Everything else — and every non-GET method —
# is a control action (POST /ask, POST /quit, DELETE /memory, the game's POSTs) and is
# refused for remote traffic.
_REMOTE_READ_PATHS = {
    "/", "/context", "/decisions", "/memory", "/activity",
    "/manifesto", "/last-prompt", "/health", "/whoami",
}


def _is_remote_view() -> bool:
    return any(h in request.headers for h in _CLOUDFLARE_MARKERS)


# ── Remote viewer presence ────────────────────────────────────────────────────
# The panel polls (no persistent socket), so "connected" = polled recently. Key on
# Cf-Connecting-Ip, which only tunnel traffic carries — the game and a local browser reach
# the server directly and are never counted. The window spans a few of the remote view's
# master-poll intervals so a viewer sitting between polls isn't dropped, while a closed tab
# ages out within a poll or two. Viewers behind one NAT'd IP collapse to a single count.
_REMOTE_MASTER_POLL_S = 3.0 * REMOTE_POLL_SCALE   # mirrors panel.html MASTER_INTERVAL (remote)
VIEWER_TTL            = max(30.0, _REMOTE_MASTER_POLL_S * 2.5)
_viewers: dict[str, float] = {}   # Cf-Connecting-Ip → last-seen epoch seconds
_viewers_lock = threading.Lock()


def _touch_viewer():
    ip = request.headers.get("Cf-Connecting-Ip")
    if not ip:
        return
    with _viewers_lock:
        _viewers[ip] = time.time()


def _viewer_count() -> int:
    cutoff = time.time() - VIEWER_TTL
    with _viewers_lock:
        stale = [ip for ip, seen in _viewers.items() if seen < cutoff]
        for ip in stale:
            del _viewers[ip]
        return len(_viewers)


@app.before_request
def _guard_remote_view():
    if _is_remote_view():
        if not ALLOW_REMOTE_VIEW:
            return "Remote access is disabled.", 403
        if request.method != "GET" or request.path not in _REMOTE_READ_PATHS:
            return "This is a read-only view.", 403
        _touch_viewer()   # counts only allowed remote reads (the panel's polls)


@app.get("/")
def panel():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if _is_remote_view():
        # Tag the body read-only (CSS hides the controls) and slow the polling loop.
        inject = f'<body class="readonly"><script>window.POLL_SCALE={REMOTE_POLL_SCALE};</script>'
        html = html.replace("<body>", inject, 1)
    # no-store so a stale (non-read-only) copy is never served from browser or edge cache.
    return html, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
    }

@app.get("/whoami")
def whoami():
    return jsonify(dict(request.headers))

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

        with _llm_lock:
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
    # Bind targets. When a specific LAN IP is configured we add an explicit 127.0.0.1 listener so
    # the game (which reaches the server on localhost) still works — binding only the LAN NIC would
    # otherwise drop loopback. "0.0.0.0" already covers loopback, so it stays a single socket.
    if HOST in ("0.0.0.0", "127.0.0.1", "localhost"):
        binds = [HOST]
    else:
        binds = [HOST, "127.0.0.1"]

    print(f"Shadow President AI server — {DISPLAY_NAME} — {':'.join(binds)}:{PORT}", flush=True)
    if HOST == "0.0.0.0":
        print("  Listening on ALL interfaces (this includes any VPN adapter). If LAN clients", flush=True)
        print("  can't reach it, set \"host\" in config.json to your LAN IP below:", flush=True)
        for ip, tag in _local_ipv4_addresses():
            print(f"    {ip}  [{tag}]   →  http://{ip}:{PORT}", flush=True)
    else:
        for h in binds:
            print(f"  Reach it at http://{h}:{PORT}", flush=True)

    # Multiple explicit binds → one werkzeug server per address (distinct local sockets, same app),
    # all but the last on daemon threads, the last on the main thread so Ctrl-C still stops it.
    from werkzeug.serving import make_server
    servers = [make_server(h, PORT, app, threaded=True) for h in binds]
    for srv in servers[:-1]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[-1].serve_forever()
