from __future__ import annotations

from .config import Project


def build_prompt(job: dict, project: Project, attachments: list[str], extra: str = "") -> str:
    parts = [
        f"你正在为项目「{project.name or project.id}」实现一个来自需求方「{job.get('requester', '')}」的需求。"
        "当前工作目录就是项目根目录，已经切换到为这个需求新建的 git 分支。",
        f"\n## 需求 #{job['task_id']}：{job['title']}\n{job['requirement']}",
    ]
    if job.get("acceptance"):
        parts.append("\n## 验收标准\n" + "\n".join(f"- {a}" for a in job["acceptance"]))
    if job.get("raw_messages"):
        parts.append("\n## 需求方原话（供参考）\n" + "\n".join(f"> {m}" for m in job["raw_messages"]))
    if attachments:
        parts.append("\n## 附件（截图等，请用 Read 工具查看）\n" + "\n".join(f"- {a}" for a in attachments))
    related = job.get("related_task")
    if related:
        parts.append(
            f"\n## 关联的上一个任务 #{related['id']}：{related['title']}\n"
            f"需求：{related['requirement'][:1500]}\n结果：{(related.get('summary') or '')[:1500]}\n"
            "本次需求是在它基础上的调整。"
        )
    if project.description or project.notes:
        parts.append(f"\n## 项目说明\n{project.description}\n{project.notes}".rstrip())
    rules = [
        "先阅读相关代码、理解现状再修改；遵循项目现有的代码风格和 CLAUDE.md 中的约定。",
        "只做需求范围内的改动，不要顺手重构无关代码，不要升级依赖版本（除非需求要求）。",
        "改完后运行项目的测试 / 类型检查 / 构建来自检" + (f"（系统随后会执行：{project.test}，必须通过）" if project.test else "") + "。",
        "不要执行 git commit / push / checkout / merge 等版本管理操作，提交和合并由系统完成。",
        "不要做部署、发布、打包上传等操作。",
        "需求有歧义时，按最合理、改动最小的理解实现，并在总结里说明你的假设。",
        "如果需求无法实现或缺少关键信息，不要硬改，直接在总结里说明原因和需要补充什么。",
        "某个命令被权限拒绝时，换别的方式或跳过即可（系统在你完成后会自动运行测试），总结里不要提权限、测试命令等技术细节。",
        "最后一条消息是写给需求方看的总结（对方可能不懂技术）：用中文，200 字以内，说明改了什么、在哪里能看到效果、有什么假设或需要确认的。不要贴代码。"
        "这条总结会原样发到企业微信，那里不显示 Markdown：只写纯文本，不要用 **加粗**、# 标题、--- 分隔线、反引号或表格；"
        "分点就用「1. 2. 3.」，也不要在总结前面加「构建通过」之类的英文或过程说明。",
    ]
    parts.append("\n## 工作要求\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)))
    if extra:
        parts.append("\n" + extra)
    return "\n".join(parts)
