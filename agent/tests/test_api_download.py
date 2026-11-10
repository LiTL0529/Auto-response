import httpx

from bridge_agent.api import ServerAPI

DATA = bytes(range(256)) * 400  # 100 KB


class Flaky(httpx.SyncByteStream):
    """先发一部分数据，然后像跨境线路那样卡住（读取超时）。"""

    def __init__(self, data: bytes, cut: int):
        self.data, self.cut = data, cut

    def __iter__(self):
        yield self.data[: self.cut]
        raise httpx.ReadTimeout("The read operation timed out")


def make_api(handler):
    api = ServerAPI("https://example.com", "t", transport=httpx.MockTransport(handler))
    api.DOWNLOAD_ATTEMPTS = 3
    return api


def test_download_resumes_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge_agent.api.time.sleep", lambda s: None)
    ranges = []

    def handler(request: httpx.Request):
        rng = request.headers.get("range")
        ranges.append(rng)
        if not rng:  # 第一次：发 30 KB 后超时
            return httpx.Response(200, headers={"content-length": str(len(DATA))}, stream=Flaky(DATA, 30_000))
        start = int(rng.split("=")[1].rstrip("-"))
        if len(ranges) == 2:  # 第二次：又发 20 KB 后超时
            return httpx.Response(206, headers={"content-range": f"bytes {start}-{len(DATA) - 1}/{len(DATA)}"},
                                  stream=Flaky(DATA[start:], 20_000))
        return httpx.Response(206, headers={"content-range": f"bytes {start}-{len(DATA) - 1}/{len(DATA)}"}, content=DATA[start:])

    seen = []
    dest = make_api(handler).download("f1", tmp_path / "a.bin", progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == DATA
    assert ranges == [None, "bytes=30000-", "bytes=50000-"]
    assert seen[-1] == (len(DATA), len(DATA))


def test_download_gives_up_when_no_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge_agent.api.time.sleep", lambda s: None)

    def handler(request):
        raise httpx.ConnectTimeout("handshake timed out")

    try:
        make_api(handler).download("f1", tmp_path / "a.bin")
    except httpx.ConnectTimeout:
        pass
    else:
        raise AssertionError("应该放弃")


def test_download_restarts_if_server_ignores_range(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge_agent.api.time.sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request.headers.get("range"))
        if len(calls) == 1:
            return httpx.Response(200, headers={"content-length": str(len(DATA))}, stream=Flaky(DATA, 10_000))
        return httpx.Response(200, content=DATA)  # 不支持 Range：整份重发

    dest = make_api(handler).download("f1", tmp_path / "a.bin")
    assert dest.read_bytes() == DATA
