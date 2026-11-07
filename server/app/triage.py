"""需求理解：用阿里云百炼（通义千问）判断意图、匹配项目、把聊天整理成结构化需求。

百炼提供 OpenAI 兼容接口：POST {base_url}/chat/completions
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

INTENTS = {"new_task", "followup", "status", "cancel", "chat"}

SYSTEM_PROMPT = """你是一个软件外包团队的需求助理。需求方会在微信里用口语化的方式提需求，你负责理解并整理成开发能直接执行的任务。

你会收到：
- projects：可用项目列表（id、名称、别名、描述）
- recent_tasks：该用户最近的任务（id、项目、标题、状态）
- history：最近的聊天记录（in=用户发来，out=我们回复）
- new_messages：用户刚刚连续发来的消息（可能被拆成多条，需要合起来理解；"[图片:xxx]" 表示一张截图）

请只输出一个 JSON 对象，字段如下：
{
  "intent": "new_task | followup | status | cancel | chat",
  "project_id": "匹配到的项目 id，无法确定则为 null",
  "related_task_id": 跟进/取消/查询的任务 id（整数）或 null,
  "title": "不超过 20 字的任务标题",
  "requirement": "整理后的完整需求：背景、要改什么、具体细节。把口语转成清晰、无歧义的描述，保留用户提到的所有具体信息（文字、颜色、位置、数值等），不要臆造用户没说的功能",
  "acceptance": ["验收标准 1", "验收标准 2"],
  "risk": "small | large",
  "wants_package": true/false,
  "reply": "需要直接回复用户的话（仅在 chat 意图或需要追问时填写，否则为空字符串）"
}

判断规则：
- new_task：提出了一个新的、可执行的修改/新功能/修 bug 需求。
- followup：对最近某个任务的补充或修改（如"刚才那个按钮再大一点"），填 related_task_id。
- status：询问进度。cancel：要求取消某个任务。
- chat：闲聊、致谢，或者需求信息不足以开工——此时在 reply 里用一两句话友好地追问缺失的关键信息（例如是哪个项目、哪个页面）。
- 项目只有一个时默认就是它；有多个且无法判断时，intent 设为 chat 并追问是哪个项目。
- risk=large：涉及数据库结构变更、删除功能/数据、大范围重构、支付/权限/安全相关、或预计改动非常大。其余为 small。
- wants_package：用户明确要求出安装包/新版本客户端时为 true。
"""


@dataclass
class TriageResult:
    intent: str
    project_id: str | None = None
    related_task_id: int | None = None
    title: str = ""
    requirement: str = ""
    acceptance: list[str] = field(default_factory=list)
    risk: str = "small"
    wants_package: bool = False
    reply: str = ""

    @classmethod
    def from_dict(cls, d: dict, project_ids: set[str]) -> "TriageResult":
        intent = d.get("intent") if d.get("intent") in INTENTS else "chat"
        pid = d.get("project_id")
        if pid not in project_ids:
            pid = None
        rid = d.get("related_task_id")
        try:
            rid = int(rid) if rid not in (None, "", "null") else None
        except (TypeError, ValueError):
            rid = None
        acceptance = d.get("acceptance") or []
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        return cls(
            intent=intent,
            project_id=pid,
            related_task_id=rid,
            title=str(d.get("title") or "")[:40],
            requirement=str(d.get("requirement") or ""),
            acceptance=[str(a) for a in acceptance][:10],
            risk="large" if d.get("risk") == "large" else "small",
            wants_package=bool(d.get("wants_package")),
            reply=str(d.get("reply") or ""),
        )


def _project_brief(projects: list[dict]) -> list[dict]:
    return [
        {"id": p["id"], "name": p.get("name", ""), "aliases": p.get("aliases", []), "description": p.get("description", "")}
        for p in projects
    ]


class Triage:
    def __init__(self, api_key: str, base_url: str, model: str, http: httpx.AsyncClient):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = http

    async def analyze(
        self,
        new_messages: list[str],
        projects: list[dict],
        history: list[dict] | None = None,
        recent_tasks: list[dict] | None = None,
    ) -> TriageResult:
        project_ids = {p["id"] for p in projects}
        if not self.api_key:
            return self.fallback(new_messages, projects)
        user_content = json.dumps(
            {
                "projects": _project_brief(projects),
                "recent_tasks": [
                    {"id": t["id"], "project_id": t["project_id"], "title": t["title"], "status": t["status"]}
                    for t in (recent_tasks or [])
                ],
                "history": [{"dir": m["direction"], "text": m["content"][:500]} for m in (history or [])],
                "new_messages": new_messages,
            },
            ensure_ascii=False,
        )
        try:
            r = await self.http.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                },
                timeout=60,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return TriageResult.from_dict(json.loads(content), project_ids)
        except Exception:  # noqa: BLE001
            log.exception("调用百炼失败，使用兜底规则")
            return self.fallback(new_messages, projects)

    @staticmethod
    def fallback(new_messages: list[str], projects: list[dict]) -> TriageResult:
        """没有配置百炼或调用失败时的简单规则：按名称/别名匹配项目，整段话当作需求。"""
        text = "\n".join(new_messages).strip()
        matched = None
        for p in projects:
            names = [p["id"], p.get("name", ""), *p.get("aliases", [])]
            if any(n and n in text for n in names):
                matched = p["id"]
                break
        if matched is None and len(projects) == 1:
            matched = projects[0]["id"]
        if matched is None:
            names = "、".join(p.get("name") or p["id"] for p in projects) or "（家里电脑还没上报项目）"
            return TriageResult(intent="chat", reply=f"请问是哪个项目的需求？可选：{names}")
        first_line = text.splitlines()[0] if text else "新需求"
        return TriageResult(
            intent="new_task",
            project_id=matched,
            title=first_line[:20],
            requirement=text,
            acceptance=[],
        )
