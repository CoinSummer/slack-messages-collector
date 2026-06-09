---
name: slack-messages-collector
description: 获取指定 Slack channel 在给定时间段内的聊天记录，并尽可能包含 thread 回复，按发言人聚合导出为 JSON。适用于收集、归档、分析或导出 Slack 频道历史消息。
---

# Slack Messages Collector

## Overview

Export Slack channel history for a requested time range as JSON grouped by speaker. Prefer the bundled script because it handles pagination, thread replies, Slack rate limits, user display names, and permalinks deterministically.

## Required Inputs

- `channel_id`: Slack conversation ID, usually `C...` for public channels or `G...` for private channels. Prefer IDs over channel names.
- `start`: start time, accepted as Unix timestamp or ISO-like datetime.
- `end`: end time, accepted as Unix timestamp or ISO-like datetime.
- `timezone`: default `Asia/Shanghai` when datetimes do not include a timezone.
- `token`: read from `SLACK_BOT_TOKEN` by default. Never print or store the token.
- `output`: JSON path to write.

Ask for missing `channel_id`, `start`, or `end`. If the user gives a channel name, first explain that a channel ID is more reliable; use Slack lookup only if a suitable Slack tool/API token is available.

## Run

Use the script from this skill directory:

```bash
python3 scripts/export_slack_channel.py \
  --channel C1234567890 \
  --start "2026-06-01 00:00:00" \
  --end "2026-06-02 00:00:00" \
  --timezone Asia/Shanghai \
  --output slack-export.json
```

If the token is stored under another environment variable:

```bash
python3 scripts/export_slack_channel.py \
  --channel C1234567890 \
  --start "2026-06-01T00:00:00+08:00" \
  --end "2026-06-02T00:00:00+08:00" \
  --token-env MY_SLACK_TOKEN \
  --output slack-export.json
```

## Output Contract

The output is JSON grouped by Slack user:

```json
{
  "channel_id": "C1234567890",
  "range": {
    "start": "2026-06-01T00:00:00+08:00",
    "end": "2026-06-02T00:00:00+08:00",
    "timezone": "Asia/Shanghai"
  },
  "speakers": {
    "U123": {
      "user_id": "U123",
      "display_name": "Alice",
      "messages": [
        {
          "datetime": "2026-06-01T10:05:00+08:00",
          "text": "Message text",
          "is_thread_reply": true,
          "permalink": "https://workspace.slack.com/archives/C123/p1780000300000100?thread_ts=1780000000.000100",
          "thread_root_permalink": "https://workspace.slack.com/archives/C123/p1780000000000100"
        }
      ]
    }
  }
}
```

Each message object must stay small: `datetime`, `text`, `is_thread_reply`, `permalink`, and `thread_root_permalink` only.

## Slack API Behavior

The script uses:

- `conversations.history` for channel messages within `oldest` and `latest`.
- `conversations.replies` for thread replies under messages returned by channel history.
- `users.info` to resolve speaker display names.
- `chat.getPermalink` to generate message and thread root permalinks.

Important limitation: Slack's normal conversation history APIs do not provide a direct way to enumerate every thread reply in a channel by time range when the thread root message is outside that range. This script reliably includes thread replies for thread roots discovered in the requested channel history window. If the user needs replies whose roots may be outside the range, state this limitation before presenting the result and discuss a broader root-message window or an export/admin API path.

## Permissions

The Slack token needs access to the conversation and the relevant history scope:

- Public channels: `channels:history`
- Private channels: `groups:history`
- DMs: `im:history`
- Group DMs: `mpim:history`
- User display names: `users:read`
- Permalinks: `links:read` is not required for `chat.getPermalink`, but the token must be able to access the message.

For private channels, the bot/app usually must be invited to the channel.

## Failure Handling

- `not_in_channel`, `channel_not_found`, or `missing_scope`: report the exact Slack error and required action.
- `invalid_auth`: ask for a valid token; do not echo the current token.
- `rate_limited`: wait for Slack's `Retry-After` and continue.
- Empty result: report that no messages were found for the exact channel and time range.

Do not summarize, redact, or transform message text unless the user explicitly asks. Do not send messages to Slack from this skill.
