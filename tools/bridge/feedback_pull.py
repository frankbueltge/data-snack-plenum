"""Feedback pull — brings Frank's rejections (with reasons) back into the engine repo.

Runs in GitHub Actions on a daily cron, never in a plenum session. Idempotent via the
committed ledger feedback/.pulled.json (seen draft ids). Stdlib only.

Only drafts with status 'rejected' AND sourceEngine 'plenum' are pulled — Key's own
studio flow is not the plenum's business.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LEDGER_REL = Path("feedback") / ".pulled.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_drafts(base_url: str, token: str) -> list[dict]:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/social/drafts",
        headers={"x-backend-token": token},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read().decode("utf-8") or "{}")
    return data.get("drafts", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--base-url", default="https://data-snack.com")
    ap.add_argument("--token", default="")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not args.token:
        print("::warning::no token provided — nothing pulled (set BACKEND_TOKEN secret)")
        return 0

    ledger_path = repo_root / LEDGER_REL
    seen = set(load_json(ledger_path, []))

    try:
        drafts = fetch_drafts(args.base_url, args.token)
    except Exception as e:
        print(f"::warning::fetch failed: {str(e)[:200]}")
        return 2

    fresh = [d for d in drafts
             if d.get("status") == "rejected"
             and d.get("sourceEngine") == "plenum"
             and d.get("id") not in seen]
    if not fresh:
        print("no new rejections")
        return 0

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fb = repo_root / "feedback" / f"{day}-rejections.md"
    lines = []
    if not fb.exists():
        lines.append(f"# Rejections — pulled {day}\n")
        lines.append("*Written by the feedback pull. These were rejected in the review dashboard — the strongest steering signal there is.*\n")
    for d in fresh:
        lines.append(f"\n## {d.get('topic', '(no topic)')}\n")
        lines.append(f"- character: {d.get('character', 'key')}")
        if d.get("snackSlug"):
            lines.append(f"- snack: {d['snackSlug']}")
        lines.append(f"- reason: **{d.get('rejectedReason', '(none given)')}**")
        for c in d.get("drafts", []):
            lines.append(f"- text: {c.get('text')}")
        seen.add(d["id"])
    with fb.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(sorted(seen), indent=2) + "\n", encoding="utf-8")
    print(f"pulled {len(fresh)} rejection(s) -> {fb.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
