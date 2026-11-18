"""Telegram Bot API (sendMessage + getUpdates long polling), standard library only."""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Awaitable, Callable

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token = token
        self.chat_id = str(chat_id)
        self.dry_run = dry_run or not token or not chat_id
        self._offset = 0
        self.sent: list[str] = []  # kept for tests / dry runs

    def _call_sync(self, method: str, params: dict, timeout: float = 35.0):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            log.error("Telegram %s failed: HTTP %s %s", method, e.code, body)
            try:
                return json.loads(body)
            except Exception:
                return {"ok": False}
        except Exception as e:  # network errors
            log.error("Telegram %s error: %s", method, e)
            return {"ok": False}

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        if self.dry_run:
            log.info("[dry-run telegram]\n%s", text)
            return True
        for chunk in _chunks(text, 3900):
            for attempt in range(3):
                res = await asyncio.to_thread(self._call_sync, "sendMessage", {
                    "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"})
                if res.get("ok"):
                    break
                retry = (res.get("parameters") or {}).get("retry_after")
                await asyncio.sleep(retry or 2 * (attempt + 1))
            else:
                return False
        return True

    async def poll_commands(self, handler: Callable[[str, list[str]], Awaitable[str]]) -> None:
        """Long-poll for /commands from the configured chat only."""
        if self.dry_run:
            return
        while True:
            res = await asyncio.to_thread(self._call_sync, "getUpdates",
                                          {"offset": self._offset, "timeout": 25, "allowed_updates": '["message"]'})
            if not res.get("ok"):
                await asyncio.sleep(5)
                continue
            for upd in res.get("result", []):
                self._offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                parts = text.split()
                cmd = parts[0].split("@")[0].lower()
                try:
                    reply = await handler(cmd, parts[1:])
                except Exception as e:  # never let a command crash the bot
                    log.exception("command failed")
                    reply = f"Error: {e}"
                if reply:
                    await self.send(reply)


def _chunks(text: str, size: int):
    if len(text) <= size:
        yield text
        return
    lines, buf = text.split("\n"), ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > size:
            yield buf
            buf = ""
        buf += ln + "\n"
    if buf:
        yield buf
