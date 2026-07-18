#!/usr/bin/env python3
"""Rewind a run's server-side log to match a game autosave you loaded by hand.

The checkpoint is the transaction commit: the game's autosave corresponds 1:1 to a `checkpoint`
line in the JSONL. When you load an older autosave in-game, the log is left ahead of the game —
every entry after that checkpoint describes a future that no longer happened. This script cuts
the log back to the matching commit so the two agree again.

Reverting to the START of turn N means keeping everything through the last checkpoint of turn
N-1, and discarding turn N onward. That is the default; use --step to cut mid-turn instead.

Four things are rewound together — the JSONL, the human transcript, memory.txt, and last_run.json.
Memory matters most: entries are turn-stamped, so anything stamped [Turn N] or later is dropped.
The per-run memory snapshot is rewritten to match, because the server copies that snapshot over
memory.txt when you answer "y" to Continue — without it, the next startup silently undoes the
revert. The manifesto is left alone (it is author-owned in human mode, and in ai mode the reset
manifesto_turn makes the server revise it again for the target turn).

Everything touched is copied to logs/backups/<run_id>_<timestamp>/ first.

Stop the server before running this, then restart it and answer "y" to Continue.

Usage:
  python revert.py --list             show every checkpoint in the active run
  python revert.py 3                  revert to the start of turn 3
  python revert.py 3 --step 5         revert to turn 3, step 5 (cut mid-turn)
  python revert.py 3 --dry-run        report what would be cut, change nothing
  python revert.py 3 --yes            skip the confirmation prompt
  python revert.py 3 --run logs/run_xxx.jsonl    operate on a run other than the active one
"""

import argparse
import json
import os
import re
import shutil
import socket
import sys
from datetime import datetime

LOGS_DIR      = "logs"
LAST_RUN_META = os.path.join(LOGS_DIR, "last_run.json")
MEMORY_FILE   = "memory.txt"
BACKUP_DIR    = os.path.join(LOGS_DIR, "backups")
SERVER_PORT   = 1954

# Memory entries are written as "[Turn N] ...". Compaction summaries ("[Summary] ...") carry no
# stamp, but they only ever summarise entries older than the ones that follow, so scanning to the
# first at-or-past-target stamp and cutting there keeps them correctly.
MEMORY_TURN_RE = re.compile(r"^\[Turn (\d+)\]")


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def server_is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", SERVER_PORT)) == 0


def load_meta() -> dict:
    if not os.path.exists(LAST_RUN_META):
        die(f"{LAST_RUN_META} not found - run this from the Server/ directory.")
    with open(LAST_RUN_META, encoding="utf-8") as f:
        return json.load(f)


def scan_checkpoints(jsonl_path: str) -> list[dict]:
    """Every checkpoint in the log, each with the byte offset of the end of its line.

    Read in binary so the offsets are true byte positions usable with truncate() — the same
    contract as the server's own checkpoint_offset.
    """
    checkpoints = []
    pos = 0
    with open(jsonl_path, "rb") as f:
        for raw in f:
            pos += len(raw)
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if entry.get("type") != "checkpoint":
                continue
            checkpoints.append({
                "turn":     entry.get("turn", 0),
                "step":     entry.get("step", 0),
                "fragment": entry.get("fragment", ""),
                "time":     entry.get("timestamp", "")[:19].replace("T", " "),
                "end":      pos,
            })
    return checkpoints


def count_entries_after(jsonl_path: str, offset: int) -> int:
    with open(jsonl_path, "rb") as f:
        f.seek(offset)
        return sum(1 for line in f if line.strip())


def list_checkpoints(jsonl_path: str) -> None:
    checkpoints = scan_checkpoints(jsonl_path)
    if not checkpoints:
        die(f"no checkpoints in {jsonl_path}")
    size = os.path.getsize(jsonl_path)
    print(f"{len(checkpoints)} checkpoints in {jsonl_path} ({size} bytes)\n")
    print(f"{'turn':>4} {'step':>4}  {'time':19}  {'offset':>9}  fragment")
    for cp in checkpoints:
        print(f"{cp['turn']:>4} {cp['step']:>4}  {cp['time']:19}  {cp['end']:>9}  {cp['fragment']}")


def trim_memory_lines(lines: list[str], turn: int) -> list[str]:
    kept = []
    for line in lines:
        m = MEMORY_TURN_RE.match(line)
        if m and int(m.group(1)) >= turn:
            break
        kept.append(line)
    return kept


def back_up(run_id: str, paths: list[str]) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"{run_id}_{stamp}")
    os.makedirs(dest, exist_ok=True)
    for p in paths:
        if p and os.path.exists(p):
            shutil.copy2(p, os.path.join(dest, os.path.basename(p)))
    return dest


def truncate_txt(txt_path: str, turn: int, step: int) -> bool:
    """Cut the transcript after the autosave banner for this checkpoint.

    Matches the banner log_checkpoint() writes. The last occurrence is the live one: a resume
    truncates the JSONL but only ever appends to the transcript, so a replayed segment can leave
    an earlier banner for the same turn/step behind.
    """
    marker = f"[AUTOSAVE — Turn {turn}, Step {step}]"
    with open(txt_path, encoding="utf-8") as f:
        text = f.read()

    idx = text.rfind(marker)
    if idx < 0:
        return False

    end_of_marker_line = text.find("\n", idx)
    if end_of_marker_line < 0:
        return False
    # The banner closes with a separator line; keep it.
    end_of_banner = text.find("\n", end_of_marker_line + 1)
    cut = len(text) if end_of_banner < 0 else end_of_banner + 1

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text[:cut])
        f.write(f"\n{'─' * 60}\n"
                f"[REVERTED to Turn {turn}, Step {step} — "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n"
                f"{'─' * 60}\n")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Rewind a run's log to a checkpoint, to match an autosave loaded by hand.")
    ap.add_argument("turn", nargs="?", type=int,
                    help="revert to the START of this turn (turn N onward is discarded)")
    ap.add_argument("--step", type=int,
                    help="cut mid-turn: keep everything before turn/step instead of the whole turn")
    ap.add_argument("--run", help="JSONL to operate on (default: the active run in last_run.json)")
    ap.add_argument("--list", action="store_true", help="list checkpoints and exit")
    ap.add_argument("--dry-run", action="store_true", help="report the cut, change nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    meta     = load_meta()
    jsonl    = args.run or meta.get("jsonl", "")
    is_active = os.path.abspath(jsonl) == os.path.abspath(meta.get("jsonl", ""))
    if not jsonl or not os.path.exists(jsonl):
        die(f"JSONL not found: {jsonl!r}")

    if args.list:
        list_checkpoints(jsonl)
        return
    if args.turn is None:
        ap.error("a turn is required (or use --list)")

    # The server holds the log open and caches the checkpoint offset in memory; cutting the file
    # underneath it puts the two out of sync again the moment it writes.
    if server_is_running():
        die(f"the server is running on port {SERVER_PORT} - stop it first, "
            f"then re-run and restart the server with Continue = y.")

    checkpoints = scan_checkpoints(jsonl)
    if not checkpoints:
        die(f"no checkpoints in {jsonl} - nothing to revert to.")

    if args.step is None:
        target  = (args.turn, 0)
        label   = f"the start of turn {args.turn}"
    else:
        target  = (args.turn, args.step)
        label   = f"turn {args.turn}, step {args.step}"

    kept = [c for c in checkpoints if (c["turn"], c["step"]) < target]
    if not kept:
        die(f"no checkpoint precedes {label} - the earliest is turn {checkpoints[0]['turn']}, "
            f"step {checkpoints[0]['step']}. Cutting to offset 0 would discard the prologue too, "
            f"so start a fresh run instead.")

    cut = kept[-1]
    size = os.path.getsize(jsonl)
    if cut["end"] >= size:
        print(f"Already at the last checkpoint (turn {cut['turn']}, step {cut['step']}) - "
              f"nothing to cut from the JSONL.")

    dropped = count_entries_after(jsonl, cut["end"])
    print(f"Run       : {meta.get('run_id', '?')}  ({jsonl})")
    print(f"Revert to : {label}")
    print(f"Last kept : checkpoint turn {cut['turn']}, step {cut['step']} - {cut['fragment']} "
          f"({cut['time']})")
    print(f"JSONL     : {size} -> {cut['end']} bytes, dropping {dropped} entries")

    memory_lines = []
    memory_drop  = 0
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            memory_lines = [l.rstrip("\n") for l in f if l.strip()]
        kept_memory = trim_memory_lines(memory_lines, args.turn)
        memory_drop = len(memory_lines) - len(kept_memory)
        print(f"Memory    : {len(memory_lines)} -> {len(kept_memory)} entries "
              f"(dropping {memory_drop} stamped [Turn {args.turn}] or later)")
        if args.step is not None and memory_drop:
            # Entries carry a turn but no step, and they are not 1:1 with checkpoints, so a
            # mid-turn cut can't be matched exactly. Dropping the whole turn errs towards the
            # model forgetting a little of what it did live through, rather than remembering a
            # future that no longer happened.
            print(f"            note: memory is turn-stamped only, so all of turn {args.turn} goes "
                  f"even though the cut is mid-turn.")

    if not is_active:
        print("Note      : this is not the active run - memory and last_run.json are left alone.")

    if args.dry_run:
        print("\n--dry-run: nothing changed.")
        return

    if not args.yes:
        print("\nProceed? [y/N]: ", end="", flush=True)
        if input().strip().lower() != "y":
            print("Aborted.")
            return

    txt   = meta.get("txt", "") if is_active else ""
    msnap = meta.get("memory_snapshot", "") if is_active else ""
    dest  = back_up(meta.get("run_id", "run"),
                    [jsonl, txt, MEMORY_FILE, msnap, meta.get("manifesto_snapshot", ""),
                     LAST_RUN_META])
    print(f"\nBacked up to {dest}")

    with open(jsonl, "r+b") as f:
        f.truncate(cut["end"])
    print(f"Truncated {jsonl} to {cut['end']} bytes.")

    if not is_active:
        print("Done. (Non-active run - memory.txt and last_run.json untouched.)")
        return

    if txt and os.path.exists(txt):
        if truncate_txt(txt, cut["turn"], cut["step"]):
            print(f"Truncated {txt} at the turn {cut['turn']}, step {cut['step']} autosave banner.")
        else:
            print(f"warning: no autosave banner for turn {cut['turn']}, step {cut['step']} in "
                  f"{txt} - transcript left as-is (it is cosmetic; the JSONL is authoritative).")

    if memory_lines:
        kept_memory = trim_memory_lines(memory_lines, args.turn)
        body = "\n".join(kept_memory) + ("\n" if kept_memory else "")
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"Trimmed {MEMORY_FILE} ({memory_drop} entries dropped).")
        # The server restores this over memory.txt on Continue — leaving it stale would undo
        # the trim above at the next startup.
        if msnap:
            with open(msnap, "w", encoding="utf-8") as f:
                f.write(body)
            print(f"Rewrote memory snapshot {msnap} to match.")

    meta["checkpoint_offset"] = cut["end"]
    # Let the manifesto be revised again for the turn we are replaying; the server skips the
    # revision when manifesto_turn is already at or past the checkpoint turn. Ignored in human mode.
    meta["manifesto_turn"] = min(meta.get("manifesto_turn", 0), max(0, args.turn - 1))
    with open(LAST_RUN_META, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    print(f"Updated {LAST_RUN_META} (checkpoint_offset={cut['end']}, "
          f"manifesto_turn={meta['manifesto_turn']}).")

    print(f"\nDone. Load the matching autosave in-game, start the server, and answer 'y' to "
          f"Continue this run.")


if __name__ == "__main__":
    main()
