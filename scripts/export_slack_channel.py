#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


API_BASE = "https://slack.com/api/"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export Slack channel messages for a time range, grouped by speaker."
    )
    parser.add_argument("--channel", required=True, help="Slack channel/conversation ID.")
    parser.add_argument("--start", required=True, help="Start time: Unix timestamp or ISO-like datetime.")
    parser.add_argument("--end", required=True, help="End time: Unix timestamp or ISO-like datetime.")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="Timezone for naive datetimes.")
    parser.add_argument("--token-env", default="SLACK_BOT_TOKEN", help="Environment variable containing Slack token.")
    parser.add_argument("--output", required=True, help="Output JSON path.")
    parser.add_argument("--limit", type=int, default=200, help="Slack page size.")
    parser.add_argument(
        "--no-threads",
        action="store_true",
        help="Do not fetch thread replies.",
    )
    parser.add_argument(
        "--no-permalinks",
        action="store_true",
        help="Skip chat.getPermalink calls and write null permalink fields.",
    )
    return parser.parse_args()


def parse_time(value, tz_name):
    value = value.strip()
    tz = ZoneInfo(tz_name)
    try:
        numeric = float(value)
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except ValueError:
        pass

    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            raise SystemExit(f"Could not parse time: {value}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def slack_call(token, method, params):
    encoded = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + method,
        data=encoded,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    while True:
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = int(exc.headers.get("Retry-After", "1"))
                time.sleep(max(retry_after, 1))
                continue
            raise

        if payload.get("ok"):
            return payload

        if payload.get("error") == "ratelimited":
            time.sleep(1)
            continue

        raise RuntimeError(f"Slack API {method} failed: {payload.get('error', 'unknown_error')}")


def paged_slack_call(token, method, params, item_key):
    cursor = ""
    while True:
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        payload = slack_call(token, method, page_params)
        for item in payload.get(item_key, []):
            yield item
        cursor = payload.get("response_metadata", {}).get("next_cursor") or ""
        if not cursor:
            break


def slack_ts_to_datetime(ts, tz_name):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(ZoneInfo(tz_name))


def text_from_message(message):
    text = message.get("text") or ""
    if text:
        return text
    blocks = message.get("blocks") or []
    parts = []
    for block in blocks:
        text_obj = block.get("text")
        if isinstance(text_obj, dict) and text_obj.get("text"):
            parts.append(text_obj["text"])
    return "\n".join(parts)


def message_user(message):
    return message.get("user") or message.get("bot_id") or message.get("username") or "unknown"


def resolve_user(token, user_id, cache):
    if user_id in cache:
        return cache[user_id]
    if user_id == "unknown" or user_id.startswith("B"):
        cache[user_id] = user_id
        return cache[user_id]
    try:
        payload = slack_call(token, "users.info", {"user": user_id})
        user = payload.get("user") or {}
        profile = user.get("profile") or {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
    except Exception:
        name = user_id
    cache[user_id] = name
    return name


def permalink(token, channel, ts, cache, enabled):
    if not enabled:
        return None
    key = (channel, ts)
    if key in cache:
        return cache[key]
    try:
        payload = slack_call(token, "chat.getPermalink", {"channel": channel, "message_ts": ts})
        value = payload.get("permalink")
    except Exception:
        value = None
    cache[key] = value
    return value


def add_grouped_message(result, user_id, display_name, message):
    speakers = result["speakers"]
    if user_id not in speakers:
        speakers[user_id] = {
            "user_id": user_id,
            "display_name": display_name,
            "messages": [],
        }
    speakers[user_id]["messages"].append(message)


def main():
    args = parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(f"Missing Slack token in environment variable {args.token_env}")

    start_dt = parse_time(args.start, args.timezone)
    end_dt = parse_time(args.end, args.timezone)
    if end_dt <= start_dt:
        raise SystemExit("--end must be after --start")

    start_ts = f"{start_dt.timestamp():.6f}"
    end_ts = f"{end_dt.timestamp():.6f}"
    include_threads = not args.no_threads
    include_permalinks = not args.no_permalinks

    result = {
        "channel_id": args.channel,
        "range": {
            "start": start_dt.astimezone(ZoneInfo(args.timezone)).isoformat(),
            "end": end_dt.astimezone(ZoneInfo(args.timezone)).isoformat(),
            "timezone": args.timezone,
        },
        "speakers": {},
    }

    user_cache = {}
    permalink_cache = {}
    seen = set()

    history_params = {
        "channel": args.channel,
        "oldest": start_ts,
        "latest": end_ts,
        "inclusive": "true",
        "limit": str(args.limit),
    }

    roots = list(paged_slack_call(token, "conversations.history", history_params, "messages"))
    roots.sort(key=lambda item: float(item.get("ts", "0")))

    for root in roots:
        root_ts = root.get("ts")
        if not root_ts or root_ts in seen:
            continue
        seen.add(root_ts)

        user_id = message_user(root)
        display_name = resolve_user(token, user_id, user_cache)
        root_permalink = permalink(token, args.channel, root_ts, permalink_cache, include_permalinks)
        add_grouped_message(
            result,
            user_id,
            display_name,
            {
                "datetime": slack_ts_to_datetime(root_ts, args.timezone).isoformat(),
                "text": text_from_message(root),
                "is_thread_reply": False,
                "permalink": root_permalink,
                "thread_root_permalink": None,
            },
        )

        if not include_threads:
            continue
        if not root.get("thread_ts") or root.get("reply_count", 0) == 0:
            continue

        reply_params = {
            "channel": args.channel,
            "ts": root_ts,
            "oldest": start_ts,
            "latest": end_ts,
            "inclusive": "true",
            "limit": str(args.limit),
        }
        replies = list(paged_slack_call(token, "conversations.replies", reply_params, "messages"))
        replies.sort(key=lambda item: float(item.get("ts", "0")))

        for reply in replies:
            reply_ts = reply.get("ts")
            if not reply_ts or reply_ts == root_ts or reply_ts in seen:
                continue
            seen.add(reply_ts)
            reply_user_id = message_user(reply)
            reply_display_name = resolve_user(token, reply_user_id, user_cache)
            add_grouped_message(
                result,
                reply_user_id,
                reply_display_name,
                {
                    "datetime": slack_ts_to_datetime(reply_ts, args.timezone).isoformat(),
                    "text": text_from_message(reply),
                    "is_thread_reply": True,
                    "permalink": permalink(token, args.channel, reply_ts, permalink_cache, include_permalinks),
                    "thread_root_permalink": root_permalink,
                },
            )

    for speaker in result["speakers"].values():
        speaker["messages"].sort(key=lambda item: item["datetime"])

    with open(args.output, "w", encoding="utf-8") as output_file:
        json.dump(result, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")

    print(
        f"Wrote {sum(len(s['messages']) for s in result['speakers'].values())} messages "
        f"from {len(result['speakers'])} speakers to {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
