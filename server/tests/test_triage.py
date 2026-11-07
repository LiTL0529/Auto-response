import asyncio
import json

import httpx

from app.triage import Triage

PROJECTS = [
    {"id": "shop", "name": "商城后台", "aliases": ["后台"]},
    {"id": "site", "name": "官网", "aliases": []},
]


def run(coro):
    return asyncio.run(coro)


def make(handler):
    async def go(msgs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            t = Triage("key", "https://dashscope.example/v1", "qwen-plus", http)
            return await t.analyze(msgs, PROJECTS)
    return go


def test_qwen_result_parsed_and_sanitized():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        content = json.dumps({
            "intent": "new_task", "project_id": "shop", "related_task_id": "null",
            "title": "登录按钮改蓝色", "requirement": "把登录页按钮改为蓝色", "acceptance": "按钮为蓝色",
            "risk": "small", "wants_package": False, "reply": "",
        }, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    r = run(make(handler)(["后台登录按钮改蓝色"]))
    assert seen["auth"] == "Bearer key"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert r.intent == "new_task" and r.project_id == "shop"
    assert r.related_task_id is None and r.acceptance == ["按钮为蓝色"]


def test_unknown_project_is_dropped():
    def handler(request):
        content = json.dumps({"intent": "new_task", "project_id": "hacker", "title": "x", "requirement": "y"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    r = run(make(handler)(["随便"]))
    assert r.project_id is None


def test_fallback_on_api_error():
    r = run(make(lambda req: httpx.Response(500))(["官网改个标题"]))
    assert r.intent == "new_task" and r.project_id == "site"
    r = run(make(lambda req: httpx.Response(500))(["改个标题"]))
    assert r.intent == "chat" and "哪个项目" in r.reply
