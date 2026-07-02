"""Enqueue bridge — delivers gate-passed queue files to the site's review queue.

Runs in GitHub Actions (full egress), never in a plenum session. Idempotent via the
committed ledger queue/.enqueued.json. Stdlib only.

Ledger entry per queue file path:
  {"status": "delivered", "draftId": "...", "at": "..."}   — done, never retried
  {"status": "vetoed", "keyword": "...", "at": "..."}      — done, never retried; feedback written
  {"status": "failed", "retries": N, "lastError": "..."}   — retried on next run

Exit codes: 0 = nothing pending or all delivered/vetoed; 2 = at least one failure this run.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LEDGER_REL = Path("queue") / ".enqueued.json"
MAX_RETRIES_BEFORE_ALARM = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ledger(repo_root: Path) -> dict:
    path = repo_root / LEDGER_REL
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(repo_root: Path, ledger: dict) -> None:
    path = repo_root / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def pending_files(repo_root: Path, ledger: dict) -> list[Path]:
    files = []
    for p in sorted((repo_root / "queue").glob("*/*.json")):
        rel = str(p.relative_to(repo_root))
        entry = ledger.get(rel)
        if entry and entry.get("status") in ("delivered", "vetoed"):
            continue
        files.append(p)
    return files


def post_enqueue(base_url: str, token: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/social/enqueue",
        data=body,
        headers={"content-type": "application/json", "x-backend-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            detail = ""
        return e.code, {"error": detail}
    except Exception as e:  # network, timeout, DNS
        return 0, {"error": str(e)[:300]}


def write_veto_feedback(repo_root: Path, rel: str, payload: dict, keyword: str) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fb = repo_root / "feedback" / f"{day}-vetoed.md"
    lines = []
    if not fb.exists():
        lines.append(f"# Server vetoes — {day}\n")
        lines.append("*Written by the enqueue bridge. The site's veto check rejected these; the plenum should learn why.*\n")
    lines.append(f"\n## {rel}\n")
    lines.append(f"- character: {payload.get('character')}")
    lines.append(f"- topic: {payload.get('topic')}")
    lines.append(f"- vetoed keyword: **{keyword}**")
    for d in payload.get("drafts", []):
        lines.append(f"- text: {d.get('text')}")
    with fb.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--base-url", default="https://data-snack.com")
    ap.add_argument("--token", default="")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not args.token:
        print("::warning::no token provided — nothing delivered (set BACKEND_TOKEN secret)")
        return 0

    ledger = load_ledger(repo_root)
    todo = pending_files(repo_root, ledger)
    if not todo:
        print("nothing pending")
        return 0

    failures = 0
    for p in todo:
        rel = str(p.relative_to(repo_root))
        payload = json.loads(p.read_text(encoding="utf-8"))
        payload.pop("sessionDate", None)  # engine bookkeeping, not part of the API contract
        status, res = post_enqueue(args.base_url, args.token, payload)

        if status == 200 and res.get("vetoed"):
            ledger[rel] = {"status": "vetoed", "keyword": res["vetoed"], "at": now_iso()}
            write_veto_feedback(repo_root, rel, payload, res["vetoed"])
            print(f"vetoed: {rel} ({res['vetoed']})")
        elif 200 <= status < 300 and res.get("id"):
            ledger[rel] = {"status": "delivered", "draftId": res["id"], "at": now_iso()}
            print(f"delivered: {rel} -> {res['id']}")
        else:
            prev = ledger.get(rel, {})
            retries = int(prev.get("retries", 0)) + 1
            ledger[rel] = {"status": "failed", "retries": retries,
                           "lastError": f"HTTP {status}: {res.get('error', json.dumps(res)[:200])}"}
            failures += 1
            print(f"::warning::failed ({retries}x): {rel} — HTTP {status}")

    save_ledger(repo_root, ledger)

    alarms = [r for r, e in ledger.items()
              if e.get("status") == "failed" and e.get("retries", 0) >= MAX_RETRIES_BEFORE_ALARM]
    if alarms:
        print("ALARM_FILES=" + ",".join(alarms))
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
