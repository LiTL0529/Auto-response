from app.textfmt import plain_text


def test_real_summary():
    src = (
        "---\n\n**总结（给需求方）**\n\n在标智软件中新增了「导出 Excel」功能，具体如下：\n\n"
        "**可导出的位置：**\n1. **项目页**（左侧「项目」菜单）——新增「导出 Excel」按钮（`项目列表.xlsx`）\n\n"
        "## 说明\n- 第一点\n* 第二点\n> 引用"
    )
    out = plain_text(src)
    for mark in ("**", "---", "`", "## ", "> "):
        assert mark not in out
    assert out.startswith("总结（给需求方）")
    assert "1. 项目页（左侧「项目」菜单）——新增「导出 Excel」按钮（项目列表.xlsx）" in out
    assert "说明\n・第一点\n・第二点\n引用" in out


def test_keeps_things_that_are_not_markdown():
    s = "任务 #12【改 C# 接口】完成，3*4=12，文件 my__file__name.txt"
    assert plain_text(s) == s
    assert plain_text("✅ 任务 #2【标题】完成") == "✅ 任务 #2【标题】完成"


def test_urls_untouched():
    url = "https://example.com/files/ab__cd/BidAgent_Setup__1.exe?exp=1&sig=a_b__c**d"
    out = plain_text(f"**安装包下载：**{url}")
    assert out == f"安装包下载：{url}"
    assert plain_text(f"[下载]({url})") == f"下载（{url}）"


def test_table_flattened():
    out = plain_text("| 页面 | 改动 |\n|---|---|\n| 项目页 | 加按钮 |")
    assert "|" not in out and "---" not in out
    assert "项目页  加按钮" in out


def test_empty():
    assert plain_text("") == ""
