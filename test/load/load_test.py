"""ACH remittance load test: replay messages from a folder against the deployed API.

Picks messages (.msg / .eml) at random from a local folder and submits each one through the real
intake path: API -> Blob `incoming/` -> Service Bus -> Durable orchestration. It creates no
documents of its own; every message sent is a file you supplied.

The intake `processingId` is a hash of the file's bytes, so a file sent twice is processed once.
To let one file count as many messages, each submission is made unique by default (--unique pad):

    .msg   512 random bytes appended after the compound file (ignored by the MSG reader; the
           parsed subject, sender, body and attachments are unchanged)
    .eml   an `X-Load-Test-Id` header added at the top

Use --unique none to send files byte-for-byte (repeats then exercise duplicate detection).

Files are reported by a short content hash, never by name, so results can be shared.

BILLABLE: every message calls Content Understanding and possibly the Foundry agents. Runs above
50 messages require --yes.

Examples:
    # The repository's synthetic set (invented data), each of the 20 scenarios once
    python test/load/load_test.py --api https://<ui-fqdn> --source-dir samples/synthetic --recursive --each-once --interval-seconds 15

    # 10 random messages, as fast as 4 senders allow
    python test/load/load_test.py --api https://<ui-fqdn> --source-dir C:/path/to/samples --count 10

    # One message every 30 s (+/- 50%), 2 at a time
    python test/load/load_test.py --api https://<ui-fqdn> --source-dir <dir> --count 40 --interval-seconds 30 --jitter 0.5

    # A day's volume compressed into one hour
    python test/load/load_test.py --api https://<ui-fqdn> --source-dir <dir> --count 1000 --duration-minutes 60 --concurrency 8 --yes

    # Each file exactly once, in random order
    python test/load/load_test.py --api https://<ui-fqdn> --source-dir <dir> --each-once

    # Show the plan without sending anything
    python test/load/load_test.py --source-dir <dir> --count 20 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CONTENT_TYPES = {".msg": "application/vnd.ms-outlook", ".eml": "message/rfc822"}
MSG_PAD_BYTES = 512


# --- selection -----------------------------------------------------------------------------------


def find_sources(source_dir: Path, patterns: list[str]) -> list[Path]:
    files = {path for pattern in patterns for path in source_dir.glob(pattern) if path.is_file()}
    return sorted(path for path in files if path.suffix.lower() in CONTENT_TYPES)


def short_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def make_unique(path: Path, data: bytes, mode: str) -> bytes:
    if mode == "none":
        return data
    if path.suffix.lower() == ".msg":
        return data + os.urandom(MSG_PAD_BYTES)
    return f"X-Load-Test-Id: {uuid.uuid4()}\r\n".encode() + data


# --- submission ----------------------------------------------------------------------------------


def submit(api_base: str, name: str, data: bytes, content_type: str, stream: str, timeout: float) -> dict:
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/api/remittances?stream={stream}",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def check_api(api_base: str, timeout: float) -> str | None:
    """None if `/api/health` answers 200, else a reason to stop before sending anything."""
    url = f"{api_base.rstrip('/')}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status == 200:
                return None
            return f"{url} returned HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        hint = " - use the UI address (it forwards /api); an internal-only API address is not reachable from outside its environment" if exc.code == 404 else ""
        return f"{url} returned HTTP {exc.code}{hint}"
    except Exception as exc:
        return f"{url} unreachable ({type(exc).__name__}) - check the address and network access (VPN for a private UI)"


def schedule(count: int, interval: float, jitter: float, rng: random.Random) -> list[float]:
    """Send offsets in seconds from the start; jitter varies each gap by +/- that fraction."""
    offsets, at = [], 0.0
    for _ in range(count):
        offsets.append(at)
        at += max(0.0, interval * (1 + rng.uniform(-jitter, jitter))) if interval else 0.0
    return offsets


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", help="Base URL of the UI (its nginx proxies /api) or the API.")
    parser.add_argument("--source-dir", required=True, type=Path, help="Folder of .msg / .eml messages to replay.")
    parser.add_argument("--pattern", default="*.msg,*.eml", help="Comma-separated globs within --source-dir (default *.msg,*.eml).")
    parser.add_argument("--recursive", action="store_true", help="Also search subfolders.")
    parser.add_argument("--count", type=int, default=10, help="Messages to send (default 10).")
    parser.add_argument("--each-once", action="store_true", help="Send every file once in random order (overrides --count).")
    pacing = parser.add_mutually_exclusive_group()
    pacing.add_argument("--interval-seconds", type=float, default=0, help="Wait between sends (default 0 = no pacing).")
    pacing.add_argument("--duration-minutes", type=float, default=0, help="Spread the sends evenly over this window.")
    parser.add_argument("--jitter", type=float, default=0.0, help="Vary each wait by +/- this fraction, 0-1 (default 0).")
    parser.add_argument("--concurrency", type=int, default=4, help="Parallel senders (default 4).")
    parser.add_argument("--unique", choices=["pad", "none"], default="pad", help="Make each send unique (default pad) or send files unchanged.")
    parser.add_argument("--stream", choices=["loadtest", "upload", "email"], default="loadtest",
                        help="Intake stream recorded on each run (default loadtest; the API accepts only these).")
    parser.add_argument("--timeout", type=float, default=60, help="HTTP timeout per submission in seconds (default 60).")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for a repeatable selection and schedule.")
    parser.add_argument("--results", type=Path, help="Write per-message results (JSON) to this file.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; send nothing.")
    parser.add_argument("--yes", action="store_true", help="Confirm billable runs above 50 messages.")
    args = parser.parse_args()

    if not args.source_dir.is_dir():
        parser.error(f"--source-dir is not a folder: {args.source_dir}")
    if not 0 <= args.jitter <= 1:
        parser.error("--jitter must be between 0 and 1")
    patterns = [p.strip() for p in args.pattern.split(",") if p.strip()]
    if args.recursive:
        patterns = [f"**/{p}" for p in patterns]
    sources = find_sources(args.source_dir, patterns)
    if not sources:
        hint = "" if args.recursive else " (add --recursive to search subfolders)"
        parser.error(f"no .msg / .eml files in {args.source_dir} match {args.pattern}{hint}")

    rng = random.Random(args.seed)
    picks = rng.sample(sources, len(sources)) if args.each_once else rng.choices(sources, k=args.count)
    count = len(picks)
    if count < 1:
        parser.error("--count must be at least 1")
    if not args.dry_run and not args.api:
        parser.error("--api is required unless --dry-run")
    if not args.dry_run and count > 50 and not args.yes:
        parser.error("runs above 50 messages call billable models; re-run with --yes")
    if args.unique == "none" and len(set(picks)) < count:
        print(f"[load] note: {count - len(set(picks))} repeat(s) with --unique none will be dropped as duplicates")

    if not args.dry_run:
        problem = check_api(args.api, args.timeout)
        if problem:
            print(f"[load] API check failed: {problem}", file=sys.stderr)
            return 2

    interval = args.duration_minutes * 60 / count if args.duration_minutes else args.interval_seconds
    offsets = schedule(count, interval, args.jitter, rng)
    print(f"[load] {count} message(s) from {len(sources)} file(s) | "
          f"{'every ~' + format(interval, '.1f') + 's' if interval else 'no pacing'}"
          f"{' +/-' + format(args.jitter * 100, '.0f') + '%' if interval and args.jitter else ''} | "
          f"concurrency {args.concurrency} | unique {args.unique} | ~{offsets[-1] / 60:.1f} min to last send")

    started = time.perf_counter()

    def task(index: int, path: Path) -> dict:
        original = path.read_bytes()
        outcome = {"seq": index + 1, "source": short_id(original), "type": path.suffix.lower().lstrip("."),
                   "plannedAtS": round(offsets[index], 1)}
        delay = started + offsets[index] - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        if args.dry_run:
            return {**outcome, "status": "planned"}
        data = make_unique(path, original, args.unique)
        t0 = time.perf_counter()
        try:
            response = submit(args.api, path.name, data, CONTENT_TYPES[path.suffix.lower()], args.stream, args.timeout)
            outcome.update(status="queued", processingId=response.get("processingId"))
        except urllib.error.HTTPError as exc:
            outcome.update(status=f"http_{exc.code}", detail=exc.read()[:200].decode(errors="replace"))
        except Exception as exc:
            outcome.update(status="error", detail=type(exc).__name__)
        outcome["sentAtS"] = round(t0 - started, 1)
        outcome["submitMs"] = int((time.perf_counter() - t0) * 1000)
        return outcome

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(task, i, path) for i, path in enumerate(picks)]
        for done, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if done % 25 == 0 or done == count:
                print(f"[load] {done}/{count} {'planned' if args.dry_run else 'submitted'} in {time.perf_counter() - started:.0f}s")

    results.sort(key=lambda r: r["seq"])
    by_status, by_source = {}, {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    submit_ms = [r["submitMs"] for r in results if "submitMs" in r]
    print(json.dumps({
        "messages": count,
        "distinctFiles": len(by_source),
        "byStatus": by_status,
        "submitP50Ms": percentile(submit_ms, 0.5),
        "submitP95Ms": percentile(submit_ms, 0.95),
        "elapsedSeconds": round(time.perf_counter() - started, 1),
    }, indent=2))
    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(results, indent=2))
        print(f"[load] results -> {args.results}")
    return 0 if all(r["status"] in ("queued", "planned") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
