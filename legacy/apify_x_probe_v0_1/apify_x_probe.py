"""ACG Intelligence Hub — Apify X timeline capability probe (staging only).

Python 3.10+, standard library only. Does NOT use X's official API.
The selected third-party Actor, its policies, and downstream data rights need
independent review before publishing, redistribution, or commercial use.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.apify.com/v2"
DEFAULT_ACTOR = "scrapesage/twitter-scraper"
DEFAULT_HANDLES = ("yoneyamai", "ui_shig", "MikaPikaZo", "Rellakinoko", "shirabii")
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}
CSV_FIELDS = ("id", "account", "published_at", "url", "post_kind", "lang", "media_count", "text_preview", "review_status")


class ApifyError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def validate_handle(value: str) -> str:
    handle = value.strip().removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        raise argparse.ArgumentTypeError(f"Invalid X username: {value!r}")
    return handle


def unique_handles(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for item in values:
        handle = validate_handle(item)
        if handle.lower() not in seen:
            result.append(handle)
            seen.add(handle.lower())
    return result


def build_actor_input(handles: list[str], days: int, max_per_account: int, include_replies: bool) -> dict:
    cutoff = utc_now() - timedelta(days=days)
    return {
        "handles": handles,
        "includeProfile": True,
        "includeTweets": True,
        "includeReplies": include_replies,
        "maxTweetsPerProfile": max_per_account,
        "onlyTweetsNewerThan": cutoff.date().isoformat(),
        "monitorMode": False,
    }


def request_json(method: str, url: str, token: str, payload: dict | None = None, timeout: int = 70):
    content = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url, data=content, method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if content is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", errors="replace")
        raise ApifyError(f"Apify HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise ApifyError(f"Network failure: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApifyError(f"Invalid JSON from Apify: {exc}") from exc


def api_data(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise ApifyError("Unexpected API wrapper; expected {'data': {...}}")
    return value["data"]


def actor_path(actor_name: str) -> str:
    """Apify accepts 'username~actor-name' or its opaque ID."""
    return quote(actor_name.replace("/", "~"), safe="~")


def start_run(token: str, actor: str, actor_input: dict, max_items: int, max_charge: float, timeout_secs: int) -> dict:
    params = urlencode({
        "maxItems": max_items,
        "maxTotalChargeUsd": f"{max_charge:.2f}",
        "timeout": timeout_secs,
    })
    url = f"{API_ROOT}/acts/{actor_path(actor)}/runs?{params}"
    # IMPORTANT: never automatically retry this POST; a timeout can hide a paid run.
    return api_data(request_json("POST", url, token, actor_input))


def await_run(token: str, run_id: str, max_wait: int) -> dict:
    deadline = time.monotonic() + max_wait
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApifyError(f"Run still active after local wait limit; inspect run ID {run_id} in Apify Console (may still incur costs)")
        wait_seconds = min(45, max(1, int(remaining)))
        params = urlencode({"waitForFinish": wait_seconds})
        state = api_data(request_json("GET", f"{API_ROOT}/actor-runs/{quote(run_id)}?{params}", token,
                                      timeout=wait_seconds + 15))
        status = str(state.get("status", ""))
        print(f"Run {run_id}: {status}")
        if status in TERMINAL:
            return state
        time.sleep(1)


def fetch_dataset(token: str, dataset_id: str, max_items: int) -> list[dict]:
    all_items = []
    offset = 0
    while offset < max_items:
        limit = min(1000, max_items - offset)
        params = urlencode({"format": "json", "clean": "false", "offset": offset, "limit": limit})
        data = request_json("GET", f"{API_ROOT}/datasets/{quote(dataset_id)}/items?{params}", token)
        if not isinstance(data, list):
            raise ApifyError("Dataset response was not a JSON array")
        all_items.extend(row for row in data if isinstance(row, dict))
        if len(data) < limit:
            break
        offset += len(data)
    return all_items


def classify_post(row: dict) -> str:
    if row.get("retweetedTweet") or row.get("isRetweet") is True or row.get("isRepost") is True:
        return "repost"
    text = row.get("text")
    if isinstance(text, str) and re.match(r"^RT\s+@\w+\s*:", text):
        return "repost"
    if row.get("isReply") is True or row.get("inReplyToTweetId"):
        return "reply"
    if row.get("isQuote") is True or row.get("quotedTweet") or row.get("quotedTweetId"):
        return "quote"
    return "original"


def normalize(row: dict, selected_handles: set[str], cutoff: datetime, save_text: bool) -> tuple[dict | None, str]:
    if row.get("type") == "profile":
        return None, "profile"
    if row.get("type") not in (None, "tweet"):
        return None, "unknown_record_type"
    post_id = row.get("id") or row.get("tweetId")
    if not post_id or not str(post_id).isdigit():
        return None, "missing_post_id"
    author = row.get("authorHandle") or row.get("authorUsername")
    if not isinstance(author, str) or author.lower().lstrip("@") not in selected_handles:
        return None, "author_mismatch"
    date = parse_date(row.get("createdAt"))
    if date is None:
        return None, "invalid_date"
    if date < cutoff:
        return None, "out_of_window"
    kind = classify_post(row)
    if kind == "repost":
        return None, "repost"
    post_id = str(post_id)
    author = author.lstrip("@")
    url = f"https://x.com/{author}/status/{post_id}"
    media = row.get("media")
    try:
        media_count = int(row.get("mediaCount") or (len(media) if isinstance(media, list) else 0))
    except (ValueError, TypeError):
        media_count = 0
    full_text = row.get("text") if isinstance(row.get("text"), str) else ""
    return {
        "id": post_id,
        "account": author,
        "published_at": date.isoformat(),
        "url": url,
        "post_kind": kind,
        "lang": str(row.get("lang") or ""),
        "media_count": media_count,
        "text_preview": full_text[:140] if save_text else "",
        "full_text": full_text if save_text else "",
        "review_status": "pending",
    }, "accepted"


def setup_db(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS staged_posts (
        id TEXT PRIMARY KEY,
        account TEXT NOT NULL,
        published_at TEXT NOT NULL,
        url TEXT NOT NULL,
        post_kind TEXT NOT NULL,
        lang TEXT,
        media_count INTEGER NOT NULL,
        text_preview TEXT,
        full_text TEXT,
        review_status TEXT NOT NULL DEFAULT 'pending',
        first_seen_at TEXT NOT NULL
    )""")
    conn.commit()


def add_to_staging(conn: sqlite3.Connection, posts: list[dict]) -> tuple[int, int]:
    inserted = 0
    seen = 0
    for post in posts:
        before = conn.total_changes
        conn.execute("""INSERT OR IGNORE INTO staged_posts
            (id, account, published_at, url, post_kind, lang, media_count,
             text_preview, full_text, review_status, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            post["id"], post["account"], post["published_at"], post["url"],
            post["post_kind"], post["lang"], post["media_count"],
            post["text_preview"], post["full_text"], post["review_status"], utc_now().isoformat(),
        ))
        if conn.total_changes > before:
            inserted += 1
        else:
            seen += 1
    conn.commit()
    return inserted, seen


def write_csv(path: Path, posts: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(posts)


def build_report(raw_items: list[dict], handles: list[str], days: int, save_text: bool) -> tuple[dict, list[dict]]:
    # ISO date from the Actor input may use day resolution, but local filter uses exact 24h window.
    cutoff = utc_now() - timedelta(days=days)
    reasons = Counter()
    counts = Counter()
    unique = {}
    for raw in raw_items:
        post, reason = normalize(raw, {h.lower() for h in handles}, cutoff, save_text)
        reasons[reason] += 1
        if post is not None:
            unique[post["id"]] = post
    posts = sorted(unique.values(), key=lambda p: (p["published_at"], p["id"]), reverse=True)
    for post in posts:
        counts[post["account"].lower()] += 1
    report = {
        "checked_at": utc_now().isoformat(),
        "requested_days": days,
        "requested_accounts": handles,
        "actor_rows_received": len(raw_items),
        "accepted_unique_posts": len(posts),
        "rejection_and_record_counts": dict(reasons),
        "by_account": {h: counts[h.lower()] for h in handles},
        "account_with_no_accepted_posts": [h for h in handles if counts[h.lower()] == 0],
        "coverage_30_day_complete": None,
        "coverage_note": "Unknown: public X timelines may cap the number of available recent posts; absence of rows is not proof of no posts.",
        "staging_only": True,
        "full_text_saved": save_text,
        "rights_note": "Third-party Actor access is not X platform permission to store, republish, redistribute, or train on content.",
    }
    return report, posts


def run(args: argparse.Namespace) -> int:
    handles = unique_handles(args.handles)
    if len(handles) > 5 and not args.allow_more_accounts:
        raise ApifyError("Safety cap: at most 5 accounts without --allow-more-accounts")
    if args.max_per_account > 50 and not args.allow_more_accounts:
        raise ApifyError("Safety cap: at most 50 posts per account without --allow-more-accounts")
    actor_input = build_actor_input(handles, args.days, args.max_per_account, args.include_replies)
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    configuration = {
        "actor": args.actor, "actor_input": actor_input,
        "run_options": {"maxItems": args.max_items, "maxTotalChargeUsd": args.max_charge_usd, "timeout": args.timeout},
        "execute": args.execute,
    }
    (output / "request_preview.json").write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(configuration, ensure_ascii=False, indent=2))
    if not args.execute:
        print("DRY RUN ONLY. No Apify run started, no fees incurred. Pass --execute to start a paid-capable run.")
        return 0
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise ApifyError("APIFY_TOKEN is missing; add it as an environment variable (never commit it)")
    if args.actor != DEFAULT_ACTOR:
        raise ApifyError("This initial version normalizes scrapesage/twitter-scraper only; do not substitute another Actor without an adapter")
    run_info = start_run(token, args.actor, actor_input, args.max_items, args.max_charge_usd, args.timeout)
    run_id = run_info.get("id")
    if not run_id:
        raise ApifyError(f"No run ID in response: {str(run_info)[:250]}")
    (output / "run_id.txt").write_text(str(run_id), encoding="utf-8")
    print(f"Started Apify run ID: {run_id}. This can incur cost up to the configured platform cap.")
    result = await_run(token, str(run_id), args.max_wait_seconds)
    (output / "run_metadata.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("status") != "SUCCEEDED":
        raise ApifyError(f"Actor did not succeed: {result.get('status')}. Inspect run {run_id}")
    dataset_id = result.get("defaultDatasetId")
    if not dataset_id:
        raise ApifyError("Successful run did not expose a defaultDatasetId")
    rows = fetch_dataset(token, str(dataset_id), args.max_items)
    report, posts = build_report(rows, handles, args.days, args.save_text)
    report["actor"] = args.actor
    report["run_id"] = str(run_id)
    report["dataset_id"] = str(dataset_id)
    report["reported_usage_total_usd"] = result.get("usageTotalUsd")
    report["reported_charged_event_counts"] = result.get("chargedEventCounts")
    with sqlite3.connect(output / "staging.sqlite3") as conn:
        setup_db(conn)
        inserted, duplicate = add_to_staging(conn, posts)
    report["new_staged_posts"] = inserted
    report["already_staged_posts"] = duplicate
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output / "review_queue.csv", posts)
    if args.save_raw:
        (output / "raw_actor_items.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nRESULT SUMMARY")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Saved staging SQLite, review CSV and report under: {output}")
    return 0


def get_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Small-sample X timeline extraction capability probe using Apify")
    parser.add_argument("--handles", nargs="+", default=list(DEFAULT_HANDLES), help="Public X usernames")
    parser.add_argument("--days", type=int, choices=range(1, 91), metavar="1..90", default=30)
    parser.add_argument("--max-per-account", type=int, choices=range(1, 201), metavar="1..200", default=30)
    parser.add_argument("--max-items", type=int, choices=range(1, 1001), metavar="1..1000", default=160)
    parser.add_argument("--max-charge-usd", type=float, default=0.50)
    parser.add_argument("--timeout", type=int, default=180, help="Apify run timeout seconds")
    parser.add_argument("--max-wait-seconds", type=int, default=240)
    parser.add_argument("--include-replies", action="store_true")
    parser.add_argument("--save-text", action="store_true", help="Store full post text in local staging DB; review rights first")
    parser.add_argument("--save-raw", action="store_true", help="Store raw Actor output locally; review rights first")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument("--allow-more-accounts", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Explicit opt-in to invoke Apify (may cost money)")
    args = parser.parse_args(argv)
    if not (0 < args.max_charge_usd <= 10.0):
        parser.error("--max-charge-usd must be >0 and <=10")
    if not 30 <= args.timeout <= 3600:
        parser.error("--timeout must be 30..3600")
    if not 30 <= args.max_wait_seconds <= 7200:
        parser.error("--max-wait-seconds must be 30..7200")
    return args


def main() -> int:
    try:
        return run(get_args())
    except (ApifyError, argparse.ArgumentTypeError, sqlite3.Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
