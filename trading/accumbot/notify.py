"""알림. 텔레그램 봇 토큰(TELEGRAM_BOT_TOKEN)과 채팅 ID(TELEGRAM_CHAT_ID)가 있으면 보내고, 없으면 콘솔에만 찍는다."""
from __future__ import annotations

import os
import sys
from datetime import datetime


def notify(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {text}", file=sys.stdout, flush=True)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        import requests

        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text},
            timeout=10,
        )
    except Exception as e:  # 알림 실패가 매매를 멈추게 하면 안 된다
        print(f"[알림 실패] {e}", file=sys.stderr)
