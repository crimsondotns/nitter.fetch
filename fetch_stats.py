import time
from typing import List, Dict, Tuple, Optional
import shared

# ==========================================
# CONSTANTS
# ==========================================
USER_BY_SCREEN_NAME_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "payments_enabled": False,
    "rweb_xchat_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

log_http_status = shared.make_status_logger("profile")

# ==========================================
# FETCHERS
# ==========================================
def fetch_user_profile(screen_name: str, row_idx: int) -> Tuple[int, str, str]:
    """Return (http_status, posts_val, followers_val) via X GraphQL."""
    status, legacy = shared.fetch_user_by_screen_name(
        screen_name, row_idx=row_idx, features=USER_BY_SCREEN_NAME_FEATURES
    )
    if status != 200:
        return status, "", f"status={status}"
    if not legacy:
        return 200, "Account suspended", "Account suspended"
    return 200, str(legacy.get("statuses_count", "")), str(legacy.get("followers_count", ""))

def fetch_community(rest_id: str, row_idx: int) -> Tuple[int, str, str]:
    status, member_count = shared.fetch_community_member_count(rest_id, row_idx=row_idx)
    if status == 200 and member_count >= 0:
        return status, "", str(member_count)
    return status, "", "rate_limited" if status == 429 else f"status={status}"

# ==========================================
# SYNC RESULTS TO GOOGLE SHEETS
# ==========================================
def sync_results_to_sheet(session_results: List[Tuple[str, List[str], List[str]]]):
    """Overwrite SHEET_NAME_STATS with fetched stats."""
    all_rows = [row_data + stats for _, row_data, stats in session_results if stats]
    if not all_rows:
        return

    shared.log_info("[SYNC] Preparing to overwrite data")
    try:
        shared.sheet_stats.batch_clear(["A2:H"])
        shared.sheet_stats.update(
            values=all_rows,
            range_name=f"A2:H{1 + len(all_rows)}",
            value_input_option="RAW",
        )
        shared.log_info(f"[SYNC] Overwrote {len(all_rows)} rows successfully")
    except Exception as e:
        shared.log_error(f"[SYNC] Sheet write error: {e!s}")
        raise

# ==========================================
# MAIN ORCHESTRATOR
# ==========================================
def get_twitter_user_stats():
    overall_start = time.perf_counter()
    daily_errors: Dict[str, Optional[dict]] = {}
    session_results: List[Tuple[str, List[str], List[str]]] = []
    consecutive_errors = 0

    engagement_rows = shared.load_engagement_rows()
    total_accounts = len(engagement_rows)
    shared.log_info(f"[APP] Initializing get_twitter_user_stats (Target: {total_accounts} accounts)")

    for idx, row_data in engagement_rows:
        if consecutive_errors >= shared.MAX_CONSECUTIVE_ERRORS:
            shared.log_error(f"[APP] Emergency stop: {shared.MAX_CONSECUTIVE_ERRORS} consecutive errors.")
            break

        ident = shared.resolve_identifier(row_data)
        if not ident:
            shared.log_warn(f"[BYPASS] Row {idx} skipped: Missing or invalid identifier")
            continue

        is_community = shared.is_rest_id(ident)
        ctx = f"Row {idx} | Community {ident}" if is_community else f"Row {idx} | @{ident}"

        try:
            shared.log_info("Fetching profile...", context=ctx)
            fetch = fetch_community if is_community else fetch_user_profile
            status, posts_val, followers_val = fetch(ident, idx)
            log_http_status(status, context=ctx)

            if status == 200:
                consecutive_errors = 0
                daily_errors[ident] = None
            else:
                if status not in (403, 404):
                    consecutive_errors += 1
                daily_errors[ident] = shared.error_entry("X API", f"HTTP {status} - https://x.com/{ident}")

            session_results.append((ident, row_data, [posts_val, followers_val]))
            shared.polite_sleep()

        except Exception as e:
            shared.log_error(f"Exception at row {idx}: {e!s}", context=ctx)
            log_http_status(500, context=ctx)
            daily_errors[ident] = shared.error_entry("Local", f"Exception: {e!s}")
            consecutive_errors += 1

    if session_results:
        sync_results_to_sheet(session_results)
    shared.sync_error_log(daily_errors)

    total_min = (time.perf_counter() - overall_start) / 60.0
    shared.log_info(
        f"[APP] Execution Summary: Processed {len(session_results)}/{total_accounts}"
        f" | Sheet: '{shared.SHEET_NAME_STATS}' | Duration: {total_min:.2f}m"
    )

if __name__ == "__main__":
    get_twitter_user_stats()
