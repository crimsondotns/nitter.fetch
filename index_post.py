import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List
import re
import os
import json
import random
import gspread
import common

NITTER_INSTANCES = [
    "nitter.net"
]

def get_nitter_instances() -> List[str]:
    try:
        resp = common.session.get("https://status.d420.de/api/v1/instances", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            valid = []
            for inst in data:
                if inst.get("rss") and inst.get("is_up"):
                    url = inst.get("url", "")
                    domain = url.replace("https://", "").replace("http://", "").rstrip("/")
                    if domain:
                        valid.append(domain)
            if len(valid) >= 3:
                return valid
    except Exception as e:
        common.log_info(f"Failed to fetch dynamic nitter instances: {e!s}")
    return NITTER_INSTANCES

def log_error_to_sheet(error_msg: str, row_idx: str, instance: str):
    try:
        sh = common.client.open_by_key(common.SPREADSHEET_ID)
        try:
            error_sheet = sh.worksheet("error.log")
        except gspread.exceptions.WorksheetNotFound:
            error_sheet = sh.add_worksheet(title="error.log", rows=1000, cols=4)
            error_sheet.append_row(["Timestamp", "Row", "Instance", "Error Message"])
        
        ts = datetime.now(common.SGT).strftime("%Y-%m-%d %H:%M:%S")
        error_sheet.append_row([ts, str(row_idx), instance, error_msg])
    except Exception as e:
        common.log_info(f"Failed to write to error.log sheet: {e!s}")

def print_custom_log(status: int, username: str):
    ts = datetime.now(common.SGT).strftime("[%a, %d %b %y %H:%M]")
    
    if status == 200:
        c_status = f"\033[92m{status}\033[0m"
        msg = f"ดำเนินการเสร็จสิ้น - ดึงข้อมูล @{username} สำเร็จ"
    elif status == 404:
        c_status = f"\033[91m{status}\033[0m"
        msg = f"ข้อผิดพลาดภายนอก - ไม่พบข้อมูลบัญชีผู้ใช้ที่ระบุ"
    elif status >= 500:
        c_status = f"\033[91m{status}\033[0m"
        msg = f"ข้อผิดพลาดระบบ - เซิร์ฟเวอร์ตอบสนองไม่ถูกต้อง (Internal Error)"
    elif status == 429:
        c_status = f"\033[93m{status}\033[0m"
        msg = f"ข้อผิดพลาดระบบ - ถูกจำกัดการเข้าถึง (Rate Limit)"
    elif status == 403:
        c_status = f"\033[91m{status}\033[0m"
        msg = f"ข้อผิดพลาดภายนอก - ถูกปฏิเสธการเข้าถึง (Forbidden)"
    else:
        c_status = f"\033[93m{status}\033[0m"
        msg = f"สถานะ - เกิดข้อผิดพลาดรหัส {status}"
        
    print(f"{ts} [STATUS {c_status}] : {msg}", flush=True)

def fetch_nitter_rss_posts(username: str, days: int = 7, row_idx: Optional[int] = None, instance: str = "nitter.net") -> Tuple[int, List[Tuple[datetime, str]]]:
    """
    ดึงโพสต์จาก Nitter RSS
    """
    url = f"https://{instance}/{username}/rss"
    path = f"/{instance}/{username}/rss"
    
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    
    try:
        resp = common.session.get(url, timeout=30)
        status = resp.status_code
        
        if status != 200:
            readable_url = f"https://x.com/{username}"
            if status == 403:
                log_error_to_sheet(f"403 Forbidden (Nitter RSS) - {readable_url}", str(row_idx), instance)
            elif status == 404:
                log_error_to_sheet(f"404 Not Found (Nitter RSS) - {readable_url}", str(row_idx), instance)
            return status, []
            
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return status, []
            
        posts: List[Tuple[datetime, str]] = []
        for item in channel.findall("item"):
            title = item.find("title")
            pub_date = item.find("pubDate")
            
            if title is not None and pub_date is not None:
                content = title.text or ""
                if content == "Image":
                    desc = item.find("description")
                    if desc is not None:
                        desc_text = desc.text
                        if desc_text:
                            m = re.search(r'src="([^"]+)"', desc_text)
                            if m:
                                content = m.group(1).replace("&amp;", "&")
                
                try:
                    dt = datetime.strptime(pub_date.text, "%a, %d %b %Y %H:%M:%S %Z")
                    dt = dt.replace(tzinfo=timezone.utc)
                    
                    if dt >= cutoff_dt:
                        full_content = f"{content}\n\n{pub_date.text}"
                        posts.append((dt, full_content))
                except Exception as e:
                    common.log_info(f"Date parse error: {e!s}", row_idx=row_idx)
                    
        posts.sort(key=lambda x: x[0], reverse=True)
        return status, posts
        
    except Exception as e:
        log_error_to_sheet(f"Nitter RSS error: {e!s}", str(row_idx), instance)
        return 500, []

def save_progress_state(state_file: str, date_str: str, processed_users: List[str]):
    try:
        new_state = {
            "date": date_str,
            "processed_users": processed_users
        }
        with open(state_file, "w") as f:
            json.dump(new_state, f)
        common.log_info(f"Saved state to {state_file} 📝 ({len(processed_users)} users completed today)")
    except Exception as e:
        common.log_info(f"Error saving state: {e!s}")

def get_twitter_user_recent_posts(days: int = 7):
    overall_start = time.perf_counter()

    STATE_FILE = "nitter_progress.json"
    state = {}
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    processed_today: List[str] = []
    
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("date") == today_str:
                processed_today = list(state.get("processed_users", []))
                common.log_info(f"Resume run detected for {today_str}. Skiping {len(processed_today)} users already processed 🚀")
            else:
                common.log_info(f"New day detected ({today_str}). Starting fresh! ☀️")
        except Exception as e:
            common.log_info(f"Error loading state: {e!s}")

    links = common.sheet_migration.col_values(1)
    total_accounts = len(links) - 1 # exclude header

    common.log_info(f"เริ่มรัน get_twitter_user_recent_posts(days={days}): total_rows_in_sheet={total_accounts}")

    instances = get_nitter_instances()
    common.log_info(f"Nitter instances ready: {len(instances)} instances.")

    # Cache original rows in case they move during execution
    # But later we fetch the freshest rows right before batch_update!
    session_results: List[Tuple[str, List[str]]] = []
    max_tweets = 0

    accounts_empty_link = 0
    accounts_tweets_api_err = 0
    total_tweets_nd = 0

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5

    idx_start = 2
    for idx_zero_based in range(idx_start - 1, len(links)):
        idx = idx_zero_based + 1 # original sheet row
        link = links[idx_zero_based]
        
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            common.log_info(f"หยุดการทำงานฉุกเฉิน เพราะ Error/Timeout ติดต่อกัน {MAX_CONSECUTIVE_ERRORS} ครั้ง 🛑")
            break

        ident_raw = common.extract_identifier_from_link(link or "")
        if not ident_raw:
            accounts_empty_link += 1
            common.log_info(f"row={idx} link ว่าง / parse ไม่ได้ → ข้าม", row_idx=idx)
            continue

        username = ident_raw.lstrip("@")
        if common.is_rest_id(username):
            common.log_info(f"skipping rest_id ident '{username}' — RSS needs screen_name", row_idx=idx)
            continue
            
        # Check if already processed today
        if username.lower() in [u.lower() for u in processed_today]:
            common.log_info(f"ข้าม @{username} (ดึงไปแล้วในวันนี้)", row_idx=idx)
            continue

        try:
            old_tag = common.ENDPOINT_TAG
            tweets = []
            success = False
            
            # Try up to 3 different instances
            attempts = 3
            for attempt in range(1, attempts + 1):
                instance = random.choice(instances)
                status_nitter, tweets = fetch_nitter_rss_posts(username, days=days, row_idx=idx, instance=instance)
                
                if status_nitter == 200:
                    success = True
                    consecutive_errors = 0
                    break
                elif status_nitter in [429, 500, 502, 503, 504]:
                    if attempt < attempts:
                        common.log_info(f"Instance {instance} failed ({status_nitter}), retrying... (Attempt {attempt}/{attempts})", row_idx=idx)
                        time.sleep(2)
                else:
                    consecutive_errors = 0
                    break
            
            if not success and status_nitter not in [200, 404, 403]:
                consecutive_errors += 1
            
            # Print our custom log once per user mapping to the final status
            print_custom_log(status_nitter, username)
            
            common.ENDPOINT_TAG = old_tag
            
            texts = [t[1] for t in tweets]
            tweet_count = len(texts)
            total_tweets_nd += tweet_count

            session_results.append((username, texts))
            max_tweets = max(max_tweets, len(texts))

            time.sleep(1.5) # Anti-rate limit delay

        except Exception as e:
            accounts_tweets_api_err += 1
            print_custom_log(500, username)
            log_error_to_sheet(f"Exception: {e!s}", str(idx), "Local")
            consecutive_errors += 1
            continue

    # Writing Phase (Only for exactly what was processed in this run)
    processed_count = len(session_results)
    if processed_count > 0:
        common.log_info("Preparing to sync and write data to sheet...")
        # Get freshest mapping of usernames to rows in case someone sorted the sheet!
        fresh_links = common.sheet_migration.col_values(1)
        fresh_map = {}
        for f_idx, f_link in enumerate(fresh_links, start=1):
            f_ident = common.extract_identifier_from_link(f_link or "")
            if f_ident:
                f_username = f_ident.lstrip("@").lower()
                fresh_map[f_username] = f_idx

        # Pad with empty strings to overwrite old data (at least 100 columns)
        target_len = max(max_tweets, 100)
        
        batch_updates = []
        successful_users_this_run = []
        
        for username, texts in session_results:
            row_idx = fresh_map.get(username.lower())
            if row_idx:
                padded = texts + [""] * (target_len - len(texts))
                batch_updates.append({
                    "range": f"E{row_idx}:ZZ{row_idx}",
                    "values": [padded]
                })
                successful_users_this_run.append(username)
            else:
                common.log_info(f"Warning: @{username} was removed from the sheet during execution, cannot write its data.")
        
        if batch_updates:
            try:
                common.sheet_migration.batch_update(batch_updates, value_input_option="RAW")
                common.log_info(f"Batch wrote {len(batch_updates)} synced rows ✅")
                
                # Expand processed valid users
                processed_today.extend(successful_users_this_run)
                save_progress_state(STATE_FILE, today_str, processed_today)
                
            except Exception as e:
                common.log_info(f"sheet write error: {e!s} ❌")
                raise
    else:
        common.log_info("No rows processed in this run.")

    total_min = (time.perf_counter() - overall_start) / 60.0
    common.log_info("===== SUMMARY (RECENT POSTS) ===")
    common.log_info(f"accounts_total={total_accounts}")
    common.log_info(f"processed_this_run={processed_count}")
    common.log_info(f"total_tweets_fetched_this_run={total_tweets_nd}")
    common.log_info(f"time_spent={total_min:.2f} นาที")
    common.log_info("===== END SUMMARY ===")

if __name__ == "__main__":
    get_twitter_user_recent_posts(30)
