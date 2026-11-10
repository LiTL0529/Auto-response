import asyncio
import time
from types import SimpleNamespace

from bridge_agent.webchat import WebChat


class SlowApi:
    """附件下载很慢的服务器：直到被取消才结束。"""

    def __init__(self):
        self.events = []

    def web_events(self, chat_id, events, attempts=8):
        self.events.extend(events)

    def download(self, file_id, dest, cancel=None, progress=None):
        for _ in range(500):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("下载已取消")
            time.sleep(0.01)
        raise AssertionError("下载没有被取消")


def test_stop_while_downloading_attachment(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "a.txt").write_text("x", encoding="utf-8")
    cfg = SimpleNamespace(web_folders=[{"path": str(folder), "name": "proj"}], claude_path="claude", claude_model="",
                          claude_effort="", use_api_key=False, max_turns=10)
    api = SlowApi()
    chat = WebChat(cfg, api, tmp_path / "web-data")

    async def main():
        await chat.handle({"op": "turn", "chat_id": "c1", "folder": str(folder), "turn": 1, "text": "看看附件",
                           "files": [{"file_id": "f1", "name": "big.pdf"}], "resume": ""})
        task = chat.turns["c1"].task
        for _ in range(300):  # 等到开始下载附件
            if any(e["type"] == "turn_started" for e in api.events):
                break
            await asyncio.sleep(0.02)
        started = time.monotonic()
        await chat.handle({"op": "stop", "chat_id": "c1"})
        await asyncio.wait_for(task, 5)
        return time.monotonic() - started

    elapsed = asyncio.run(main())
    types = [e["type"] for e in api.events]
    assert elapsed < 3
    assert "turn_started" in types and {"type": "notice", "text": "已停止"} in api.events
    assert types[-1] == "status" and api.events[-1]["status"] == "idle"
    assert "c1" not in chat.turns
