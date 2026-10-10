import os
import time
import re
import random
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict
import shared

# ==========================================
# CONSTANTS
# ==========================================
# Comma-separated hostnames from env / GitHub secret, e.g. "nitter.a.com,nitter.b.com"
NITTER_INSTANCES     = [h.strip() for h in os.getenv("NITTER_INSTANCES", "").split(",") if h.strip()]
NITTER_TIMEOUT       = 15
NITTER_429_COOLDOWN  = (30.0, 60.0)
DEFAULT_DAYS         = 30
CHUNK_SIZE           = 500

log_http_status = shared.make_status_logger("posts")

# Separate session for Nitter: never leak X bearer/cookies to a third-party host.
nitter_session = requests.Session()
nitter_session.headers.update({
    "User-Agent":      shared.session.headers["User-Agent"],
    "Accept":          "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

# ==========================================
# NITTER RSS FETCHER
# ==========================================
def fetch_recent_tweets_nitter(username: str, days: int = DEFAULT_DAYS) -> Tuple[int, List[Tuple[str, str]]]:
    """Fetch recent tweets via Nitter  feed.

    Returns:
        (http_status, [(date_str, content), ...])
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    tweets: List[Tuple[str, str]] = []

    instances = list(NITTER_INSTANCES)
    random.shuffle(instances)

    for i, instance in enumerate(instances, start=1):
        url = f"https://{instance}/{username}/rss"
        try:
            resp = nitter_session.get(url, timeout=NITTER_TIMEOUT)
            if resp.status_code == 429:
                shared.log_warn(f"[NET] Nitter #{i} rate-limited, cooling down...")
                shared.polite_sleep(NITTER_429_COOLDOWN)
                continue
            if resp.status_code != 200:
                if resp.status_code in (403, 404):
                    return resp.status_code, []
                continue

            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            if channel is None:
                continue

            for item in channel.findall("item"):
                title    = item.find("title")
                pub_date = item.find("pubDate")
                link     = item.find("link")
                if title is None or pub_date is None:
                    continue

                content = title.text or ""
                if content == "Image":
                    desc = item.find("description")
                    if desc is not None and desc.text:
                        m = re.search(r'src="([^"]+)"', desc.text)
                        if m:
                            content = m.group(1).replace("&amp;", "&")

                source_url = link.text.strip().split("#")[0] if link is not None and link.text else ""

                try:
                    dt = datetime.strptime(
                        pub_date.text, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc)
                    if dt >= cutoff:
                        source = f"Source: {source_url}" if source_url else ""
                        parts = [content, source, pub_date.text]
                        tweets.append((dt.strftime("%Y-%m-%d"), "\n\n".join(p for p in parts if p)))
                except Exception:
                    continue

            tweets.sort(key=lambda x: x[0], reverse=True)
            return 200, tweets

        except Exception:
            continue

    return 500, []

# ==========================================
# X API EXISTENCE CHECK (fallback when Nitter fails)
# ==========================================
def check_user_exists_on_x(username: str, row_idx: int) -> int:
    """Quick check via UserByScreenName to distinguish 404 vs rate-limit.

    Returns:
        Effective status code: 429 (user exists, Nitter issue) or 404 / other.
    """
    try:
        status, legacy = shared.fetch_user_by_screen_name(username, row_idx=row_idx)
    except Exception:
        return 500
    if status != 200:
        return status
    return 429 if legacy else 404

# ==========================================
# SYNC RESULTS TO GOOGLE SHEETS
# ==========================================
def sync_results_to_sheet(session_results: List[Tuple[str, List[str], List[str]]]):
    """Overwrite SHEET_NAME_POSTS with fetched posts."""
    all_rows: List[List[str]]  = []
    max_tweets = 0
    total_posts = 0

    for _, row_data, texts in session_results:
        if not texts:
            continue
        all_rows.append(row_data + texts)
        max_tweets = max(max_tweets, len(texts))
        total_posts += len(texts)

    if not all_rows:
        return 0

    full_width = shared.ENGAGEMENT_COL_COUNT + max_tweets

    shared.log_info("[SYNC] Preparing to overwrite data")
    try:
        shared.sheet_posts.batch_clear(["A2:ZZ"])

        total_chunks = (len(all_rows) + CHUNK_SIZE - 1) // CHUNK_SIZE
        for i in range(0, len(all_rows), CHUNK_SIZE):
            chunk     = all_rows[i : i + CHUNK_SIZE]
            start_row = i + 2
            end_row   = start_row + len(chunk) - 1
            last_col  = shared.gspread.utils.rowcol_to_a1(1, full_width).rstrip("1")
            range_name = f"A{start_row}:{last_col}{end_row}"

            shared.sheet_posts.update(
                values=chunk, range_name=range_name, value_input_option="RAW"
            )
            shared.log_info(
                f"[SYNC] Written chunk {i // CHUNK_SIZE + 1}/{total_chunks} "
                f"({len(chunk)} users) to '{shared.SHEET_NAME_POSTS}'!{range_name}"
            )

        shared.log_info(f"[SYNC] Overwrote {len(all_rows)} rows successfully")
    except Exception as e:
        shared.log_error(f"[SYNC] Sheet write error: {e!s}")
        raise

    # Write completion timestamp
    final_ts = shared.now_str()
    try:
        shared.sheet_posts.update(values=[[final_ts]], range_name="G1", value_input_option="RAW")
        shared.log_info(f"[SYNC] Final timestamp written: {final_ts}")
    except Exception as e:
        shared.log_error(f"[SYNC] Failed to write timestamp: {e!s}")

    return total_posts

# ==========================================
# MAIN ORCHESTRATOR
# ==========================================
def get_twitter_user_recent_posts(days: int = DEFAULT_DAYS):
    overall_start = time.perf_counter()
    daily_errors: Dict[str, Optional[dict]] = {}
    session_results: List[Tuple[str, List[str], List[str]]] = []
    consecutive_errors = 0
    total_tweets = 0

    engagement_rows = shared.load_engagement_rows()
    total_accounts  = len(engagement_rows)
    shared.log_info(f"[APP] Initializing get_twitter_user_recent_posts (Target: {total_accounts} accounts)")
    if not NITTER_INSTANCES:
        shared.log_error("[APP] NITTER_INSTANCES is empty. Set it in .env or GitHub secrets.")
        return
    shared.log_info(f"[NET] Nitter instances active: {len(NITTER_INSTANCES)} servers")

    for idx, row_data in engagement_rows:
        if consecutive_errors >= shared.MAX_CONSECUTIVE_ERRORS:
            shared.log_error(f"[APP] Emergency stop: {shared.MAX_CONSECUTIVE_ERRORS} consecutive errors.")
            break

        username = shared.resolve_identifier(row_data)
        if not username:
            shared.log_warn(f"[BYPASS] Row {idx} skipped: Missing or invalid identifier")
            continue

        if shared.is_rest_id(username):
            shared.log_info(f"[BYPASS] Row {idx} skipped: Community {username}")
            continue

        ctx = f"Row {idx} | @{username}"
        try:
            shared.log_info("Fetching posts...", context=ctx)
            status, tweets = fetch_recent_tweets_nitter(username, days=days)

            # Nitter failed → check if account actually exists on X
            if status != 200:
                shared.log_warn(f"Nitter failed ({status}), checking X API existence...", context=ctx)
                status = check_user_exists_on_x(username, row_idx=idx)

            log_http_status(status, context=ctx)

            # Track errors
            if status == 200:
                consecutive_errors = 0
                daily_errors[username] = None
            else:
                if status not in (403, 404):
                    consecutive_errors += 1
                daily_errors[username] = shared.error_entry(
                    "Nitter/X-API", f"HTTP {status} - https://x.com/{username}"
                )

            texts = [t[1] for t in tweets]
            total_tweets += len(texts)
            session_results.append((username, row_data, texts))
            shared.polite_sleep()

        except Exception as e:
            shared.log_error(f"Exception at row {idx}: {e!s}", context=ctx)
            log_http_status(500, context=ctx)
            daily_errors[username] = shared.error_entry("Local", f"Exception: {e!s}")
            consecutive_errors += 1

    # --- Sync ---
    if session_results:
        total_tweets += sync_results_to_sheet(session_results)
    shared.sync_error_log(daily_errors)

    total_min = (time.perf_counter() - overall_start) / 60.0
    processed = len(session_results)
    shared.log_info(
        f"[APP] Execution Summary: Processed {processed}/{total_accounts}"
        f" | Sheet: '{shared.SHEET_NAME_POSTS}'"
        f" | Posts: {total_tweets} | Duration: {total_min:.2f}m"
    )

if __name__ == "__main__":
    get_twitter_user_recent_posts()
