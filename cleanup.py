import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Enable ANSI colors on Windows 10+
if os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value |= 0x0004
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass


RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[35m"


def color(text: str, code: str) -> str:
    return f"{code}{text}{RESET}"


BASE_URL = "https://discord.com/api/v10"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
})
SESSION.mount(
    "https://",
    HTTPAdapter(
        pool_maxsize=200,
        max_retries=Retry(
            total=4,
            backoff_factor=0.8,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "DELETE"],
        ),
    ),
)


def get_headers(token: str) -> dict:
    return {"Authorization": token}


def api_request(method: str, endpoint: str, token: str, **kwargs) -> Optional[requests.Response]:
    attempts = 0
    backoff = 1.0
    max_attempts = 6
    while attempts < max_attempts:
        try:
            headers = kwargs.pop("headers", {})
            headers.update(get_headers(token))
            resp = SESSION.request(method, BASE_URL + endpoint, headers=headers, timeout=(3, 8), **kwargs)
            if resp is None:
                return None
            if resp.status_code == 429:
                # Rate limited — respect Retry-After or JSON retry_after
                retry_after = None
                try:
                    j = resp.json()
                    retry_after = j.get("retry_after")
                except Exception:
                    pass
                if retry_after is None:
                    retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                time.sleep(min(wait + 0.2, 60.0))
                attempts += 1
                backoff *= 2
                continue
            return resp
        except requests.RequestException as exc:
            attempts += 1
            time.sleep(backoff)
            backoff *= 2
            if attempts >= max_attempts:
                print(color(f"[ERROR] HTTP request failed after retries: {exc}", RED))
                return None
    return None


def validate_token(token: str) -> Optional[dict]:
    response = api_request("GET", "/users/@me", token)
    if response is None or response.status_code != 200:
        return None
    return response.json()


def get_guild_channels(token: str, guild_id: int) -> Optional[List[dict]]:
    response = api_request("GET", f"/guilds/{guild_id}/channels", token)
    if response is None or response.status_code != 200:
        return None
    try:
        return [
            channel for channel in response.json()
            if channel.get("type") in {0, 5, 10, 11, 12, 15}
        ]
    except ValueError:
        return None


def get_channel(token: str, channel_id: int) -> Optional[dict]:
    response = api_request("GET", f"/channels/{channel_id}", token)
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def open_dm_channel(token: str, user_id: int) -> Optional[dict]:
    response = api_request("POST", "/users/@me/channels", token, json={"recipient_id": str(user_id)})
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def delete_message(token: str, channel_id: int, message_id: str) -> bool:
    response = api_request("DELETE", f"/channels/{channel_id}/messages/{message_id}", token)
    return response is not None and response.status_code in {200, 204}


def count_my_messages_in_channel(token: str, channel_id: int, user_id: str, limit: float) -> int:
    count = 0
    before = None
    while count < limit:
        params = {"limit": 100}
        if before:
            params["before"] = before
        response = api_request("GET", f"/channels/{channel_id}/messages", token, params=params)
        if response is None or response.status_code != 200:
            break
        try:
            batch = response.json()
        except ValueError:
            break
        if not batch:
            break

        for msg in batch:
            author = msg.get("author", {})
            if author.get("id") == user_id:
                count += 1
                if count >= limit:
                    return count

        if len(batch) < params["limit"]:
            break
        before = batch[-1].get("id")
        if not before:
            break
    return count


def delete_my_messages_in_channel(
    token: str,
    channel_id: int,
    label: str,
    user_id: str,
    limit: float,
    confirm: bool,
    global_remaining: Optional[dict] = None,
    remaining_lock: Optional[threading.Lock] = None,
) -> int:
    deleted_count = 0
    before = None
    while deleted_count < limit:
        params = {"limit": 100}
        if before:
            params["before"] = before
        response = api_request("GET", f"/channels/{channel_id}/messages", token, params=params)
        if response is None or response.status_code != 200:
            break
        try:
            batch = response.json()
        except ValueError:
            break
        if not batch:
            break

        for msg in batch:
            author = msg.get("author", {})
            if author.get("id") != user_id:
                continue

            if global_remaining is not None and remaining_lock is not None:
                with remaining_lock:
                    if global_remaining["count"] <= 0:
                        return deleted_count

            if not confirm:
                deleted_count += 1
                if global_remaining is not None and remaining_lock is not None:
                    with remaining_lock:
                        global_remaining["count"] -= 1
                continue

            success = delete_message(token, channel_id, msg.get("id"))
            if success:
                deleted_count += 1
                print(color(f'Done of delete this massges "{format_message_preview(msg)}"', GREEN))
                if global_remaining is not None and remaining_lock is not None:
                    with remaining_lock:
                        global_remaining["count"] -= 1
            else:
                print(color(f"[ERROR] Failed to delete message {msg.get('id')}.", RED))

            if global_remaining is not None and remaining_lock is not None:
                with remaining_lock:
                    if global_remaining["count"] <= 0:
                        return deleted_count

        if len(batch) < params["limit"]:
            break
        before = batch[-1].get("id")
        if not before:
            break

    return deleted_count


def format_message_preview(message: dict) -> str:
    content = message.get("content") or "[empty message]"
    if len(content) > 60:
        content = content[:60] + "..."
    content = content.replace("\n", " ")
    timestamp = message.get("timestamp", "unknown")
    return f"ID: {message.get('id')} | {timestamp} | {content}"


def cleanup_channel(token: str, channel_id: int, label: str, user_id: str, limit: int, confirm: bool) -> int:
    return delete_my_messages_in_channel(token, channel_id, label, user_id, limit, confirm)


def cleanup_guild(token: str, guild_id: int, user_id: str, limit: int, confirm: bool, skip_channels: List[str], workers: int) -> int:
    channels = get_guild_channels(token, guild_id)
    if channels is None:
        print(color("[ERROR] Cannot fetch guild channels or invalid server ID.", RED))
        return 0

    skip_set = {item.lower() for item in skip_channels}
    selected_channels = []
    for channel in channels:
        channel_id = str(channel.get("id", ""))
        channel_name = channel.get("name", "").lower()
        if channel_id in skip_set or channel_name in skip_set:
            continue
        selected_channels.append(channel)

    if not selected_channels:
        print(color("[ERROR] No readable text channels found.", RED))
        return 0

    deleted_count = 0
    global_remaining = {"count": limit}
    remaining_lock = threading.Lock()
    max_workers = min(max(1, min(workers, 8)), len(selected_channels))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                delete_my_messages_in_channel,
                token,
                channel.get("id"),
                "",
                user_id,
                limit,
                confirm,
                global_remaining,
                remaining_lock,
            ): channel
            for channel in selected_channels
        }
        for future in as_completed(futures):
            try:
                deleted_count += future.result()
            except Exception as exc:
                channel = futures[future]
                print(color(f"[ERROR] Cleanup failed for channel {channel.get('id')}: {exc}", RED))
    return deleted_count


def cleanup_group_dm(token: str, channel_id: int, user_id: str, limit: int, confirm: bool) -> int:
    channel = get_channel(token, channel_id)
    if channel is None:
        print(color("[ERROR] Group DM channel not found.", RED))
        return 0
    return cleanup_channel(token, channel_id, "Group DM", user_id, limit, confirm)


def cleanup_dm(token: str, user_id_target: int, user_id: str, limit: float, confirm: bool) -> int:
    channel = open_dm_channel(token, user_id_target)
    if channel is None:
        print(color("[ERROR] Could not open DM conversation.", RED))
        return 0
    return cleanup_channel(token, channel.get("id"), f"DM with {user_id_target}", user_id, limit, confirm)


def count_guild_messages(token: str, guild_id: int, user_id: str, limit: float, skip_channels: List[str], workers: int) -> int:
    channels = get_guild_channels(token, guild_id)
    if channels is None:
        return 0

    skip_set = {item.lower() for item in skip_channels}
    selected_channels = [channel for channel in channels
                         if str(channel.get("id", "")) not in skip_set
                         and channel.get("name", "").lower() not in skip_set]
    if not selected_channels:
        return 0

    max_workers = min(max(1, min(workers, 8)), len(selected_channels))
    total = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                count_my_messages_in_channel,
                token,
                channel.get("id"),
                user_id,
                limit,
            ): channel
            for channel in selected_channels
        }
        for future in as_completed(futures):
            try:
                total += future.result()
                if total >= limit:
                    return int(limit)
            except Exception:
                continue
    return min(int(limit), total)


def count_group_messages(token: str, channel_id: int, user_id: str, limit: float) -> int:
    channel = get_channel(token, channel_id)
    if channel is None:
        return 0
    return count_my_messages_in_channel(token, channel_id, user_id, limit)


def count_dm_messages(token: str, user_id_target: int, user_id: str, limit: float) -> int:
    channel = open_dm_channel(token, user_id_target)
    if channel is None:
        return 0
    return count_my_messages_in_channel(token, channel.get("id"), user_id, limit)


def write_report(deleted_count: int, mode: str, target_id: int, start_time: float) -> None:
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)
    time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
    report_path = os.path.join(os.path.dirname(__file__), "cleanup_report.txt")
    report = (
        f"{'-'*50}\n"
        f"Discord Message Cleanup - Success Report\n"
        f"{'-'*50}\n"
        f"Date and Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Deletion Mode: {mode}\n"
        f"Target ID: {target_id}\n"
        f"Total Messages Deleted: {deleted_count}\n"
        f"Execution Time: {time_str}\n"
        f"Status: Completed\n"
        f"{'-'*50}\n"
    )
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(report)
    print(color(f"[OK] Report saved to: {report_path}", MAGENTA))


def print_completion(deleted_count: int, start_time: float) -> None:
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)
    time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
    print(color("\n" + "="*50, BOLD + GREEN))
    print(color("[OK] Operation completed successfully!", BOLD + GREEN))
    print(color(f"[OK] Total messages deleted: {deleted_count}", GREEN))
    print(color(f"[OK] Execution time: {time_str}", GREEN))
    print(color("[OK] Tool is shutting down", GREEN))
    print(color("="*50 + "\n", BOLD + GREEN))


def clear_console() -> None:
    try:
        if os.name == "nt":
            os.system("cls")
        else:
            os.system("clear")
    except Exception:
        pass


def normalize_token(token: str) -> str:
    if token is None:
        return ""
    value = token.strip()
    if value.lower().startswith("bot "):
        value = value[4:].strip()
    if value.lower().startswith("token "):
        value = value[6:].strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].strip()
    return value


def get_discord_creation_date(user_id: str) -> str:
    try:
        timestamp = ((int(user_id) >> 22) + 1420070400000) / 1000.0
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "unknown"


def wait_for_exit(message: str = "Press Enter to exit...") -> None:
    print(color(f"\n{message}", YELLOW))
    try:
        input()
    except EOFError:
        pass


def prompt_for_token() -> str:
    while True:
        token = input("Enter Discord token: ").strip()
        if token:
            return token
        print(color("[ERROR] Token cannot be empty. Please paste your Discord token.", RED))


def choose_delete_mode(existing_mode: Optional[str] = None) -> str:
    if existing_mode in {"server", "group", "dm"}:
        return existing_mode

    print(color("\n[STEP 2] Choose deletion mode", BOLD + CYAN))
    print(color("1 - Private DM", CYAN))
    print(color("2 - Group DM", CYAN))
    print(color("3 - Server", CYAN))

    while True:
        choice = input("Select 1/2/3: ").strip().lower()
        if choice in {"1", "dm", "private", "private dm"}:
            return "dm"
        if choice in {"2", "group", "group dm"}:
            return "group"
        if choice in {"3", "server", "guild"}:
            return "server"
        print(color("[ERROR] Please choose 1, 2 or 3.", RED))


def prompt_for_target_id(mode: str, existing_id: Optional[int] = None) -> int:
    if existing_id is not None:
        return existing_id

    if mode == "server":
        prompt = "Server ID: "
    elif mode == "group":
        prompt = "Group DM channel ID: "
    else:
        prompt = "User ID for DM: "

    while True:
        value = input(prompt).strip()
        if value.isdigit():
            return int(value)
        print(color("[ERROR] Please enter a valid numeric ID.", RED))


def prompt_for_skip_channels(default_skip: List[str]) -> List[str]:
    if default_skip:
        return default_skip

    raw = input("Skip channel IDs/names (comma-separated, leave blank to skip none): ").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def prompt_for_confirmation() -> bool:
    print(color("\n[STEP 4] Confirm deletion", BOLD + CYAN))
    print(color("Type DELETE to confirm and continue.", YELLOW))
    value = input("Enter confirmation: ").strip()
    return value.upper() == "DELETE"


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord message cleanup tool")
    parser.add_argument("--token", help="Discord token")
    parser.add_argument("--mode", choices=["server", "group", "dm"], help="Deletion mode")
    parser.add_argument("--target-id", type=int, help="Server, group DM, or user ID to target")
    parser.add_argument("--limit", type=int, default=1000,
                        help="Max number of messages to delete")
    parser.add_argument("--all", action="store_true",
                        help="Delete all matching messages regardless of age or count")
    parser.add_argument("--workers", type=int, default=8,
                        help="Number of concurrent channel workers for server cleanup")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete messages instead of previewing")
    parser.add_argument("--skip-channels", nargs="*", default=[],
                        help="List of channel IDs or names to skip")
    args = parser.parse_args()

    clear_console()
    print(color("=== Discord Cleanup Tool ===", BOLD + MAGENTA))
    print(color("[STEP 1] Login with token", BOLD + CYAN))
    token = normalize_token(args.token or prompt_for_token())

    account_data = validate_token(token)
    if account_data is None:
        print(color("[ERROR] Invalid or expired token. Please try again.", RED))
        wait_for_exit()
        return

    delete_limit = float("inf") if args.all else args.limit

    clear_console()
    username = account_data.get("username", "unknown")
    discriminator = account_data.get("discriminator", "0000")
    user_tag = f"{username}#{discriminator}"
    user_id = account_data.get("id", "unknown")
    creation_date = get_discord_creation_date(user_id)

    print(color("=== Login successful ===", BOLD + GREEN))
    print(color("[OK] Discord token validated successfully.", GREEN))
    print(color("[OK] Login confirmed for this account.", GREEN))
    print(color(f"Account: {user_tag}", GREEN))
    print(color(f"User ID: {user_id}", GREEN))
    print(color(f"Created: {creation_date}", GREEN))

    mode = choose_delete_mode(args.mode)
    target_id = prompt_for_target_id(mode, args.target_id)
    skip_list = prompt_for_skip_channels(args.skip_channels)

    if not args.confirm:
        confirmed = prompt_for_confirmation()
        if not confirmed:
            print(color("[ERROR] Operation cancelled.", RED))
            return

    clear_console()
    print(color("Counting messages...", BOLD + CYAN))
    if mode == "server":
        total_messages = count_guild_messages(token, target_id, user_id, delete_limit, skip_list, args.workers)
    elif mode == "group":
        total_messages = count_group_messages(token, target_id, user_id, delete_limit)
    else:
        total_messages = count_dm_messages(token, target_id, user_id, delete_limit)

    print(color(f"Amount of messages (عدد رسايل): {total_messages}", BOLD + GREEN))
    if total_messages == 0:
        print(color("[ERROR] No messages found to delete.", RED))
        wait_for_exit()
        return

    clear_console()
    print(color("Starting cleanup...", BOLD + CYAN))
    start_time = time.time()

    deleted_count = 0
    if mode == "server":
        deleted_count = cleanup_guild(token, target_id, user_id, delete_limit, True, skip_list, args.workers)
    elif mode == "group":
        deleted_count = cleanup_group_dm(token, target_id, user_id, delete_limit, True)
    else:
        deleted_count = cleanup_dm(token, target_id, user_id, delete_limit, True)

    write_report(deleted_count, mode, target_id, start_time)
    print_completion(deleted_count, start_time)
    wait_for_exit()


if __name__ == "__main__":
    main()
