# 部署指南

整体顺序：**企业微信拿到各种 ID 和密钥 → 部署服务器 → 回到企业微信保存回调地址 → 安装家里电脑的 Agent → 微信里测试**。

> 企业微信后台的菜单和规则经常调整，下面的路径如果对不上，以后台实际提示为准。

---

## 第一步：企业微信

### 1.1 注册企业

到 <https://work.weixin.qq.com> 注册。个人也可以注册（填个团队名），未认证状态就能用自建应用。

记下：**我的企业 → 企业信息 → 企业ID**（`WECOM_CORP_ID`）。

### 1.2 创建自建应用「需求助手」

**应用管理 → 自建 → 创建应用**：

- 可见范围：选你自己（方案 A 里还要加上朋友）
- 创建后记下 **AgentId**（`WECOM_AGENT_ID`）和 **Secret**（`WECOM_APP_SECRET`，在企业微信 App 里查看）
- **企业可信IP**：填阿里云 ECS 的公网 IP（不填的话发消息会报 60020 错误）
- **接收消息 → 设置 API 接收**：
  - URL：`https://你的域名/wecom/callback`
  - Token、EncodingAESKey：点「随机获取」，分别填到 `WECOM_CALLBACK_TOKEN`、`WECOM_ENCODING_AES_KEY`
  - **先别点保存**。保存时企业微信会请求这个 URL 做校验，要等第二步把服务器部署好才能通过

再记下你自己的账号：**通讯录 → 点自己 → 账号**（`OWNER_USERID`）。

### 1.2.1 多个应用（可选）

想按项目分开（比如「需求助手」管桌面软件、「智能党校」只管党校平台），可以再建几个自建应用：

1. 照 1.2 再创建一个自建应用，记下 AgentId，Secret 在企业微信 App 里查看
2. 给它起一个渠道名（小写字母/数字，如 `dx`），接收消息的 URL 填 `https://你的域名/wecom/callback/dx`，Token / EncodingAESKey 随机获取
3. 在服务器 `deploy/.env` 里加上 `WECOM_APPS=dx` 和一组 `WECOM_DX_*`（见 `.env.example`），`WECOM_DX_PROJECTS` 填这个应用负责的项目 id
4. `systemctl restart auto-response` 后回到企业微信保存回调；企业可信 IP 每个应用都要单独配置

每个应用只处理自己的项目，回复、审批（「同意 N」）和部署结果都在同一个应用里。主应用默认处理全部项目，可以用 `WECOM_PROJECTS` 限制。朋友在每个应用里需要分别授权。

### 1.3 让朋友接入（二选一）

**方案 A：朋友加入你的企业（最简单，推荐）**

1. 通讯录 → 添加成员，邀请朋友加入
2. 把「需求助手」应用的可见范围加上朋友
3. 朋友在企业微信 App → 工作台 → 需求助手，直接发消息提需求

没有消息条数限制，体验最稳定。

**方案 B：微信客服（朋友不用装企业微信，直接在个人微信里聊）**

1. 应用管理 → 微信客服 → 创建一个客服账号
2. 微信客服 → API → 把这个客服账号交给「需求助手」自建应用管理（企业内部开发接入）
3. 复制客服账号的**客服链接**发给朋友，朋友在个人微信里打开就能发消息

限制：朋友发一条消息后，48 小时内系统最多回复 5 条。所以系统给朋友的回复都尽量合并成一条。

### 1.4 授权朋友

朋友第一次发消息时，你会在企业微信收到提示：

```
有未授权用户发来消息：……
如果是自己人，回复：
授权 kf:wmXXXXXX 名字
```

把这句话复制回复给「需求助手」，朋友就能用了。

---

## 第二步：阿里云 ECS（Ubuntu 22.04 为例）

### 2.1 准备

- 安全组开放 80、443 端口
- 域名解析：`bot.你的域名.com` 加一条 A 记录，指向 ECS 公网 IP
- 证书：阿里云 **数字证书管理服务 → SSL 证书 → 免费证书**，申请后下载 **Nginx** 格式

### 2.2 安装

```bash
sudo apt update && sudo apt install -y python3-venv nginx git
sudo useradd -r -s /usr/sbin/nologin bridge
sudo git clone https://github.com/LiTL0529/Auto-response.git /opt/auto-response
```

> 国内 ECS 访问 GitHub 慢的话，可以在本地下载 zip 后用 scp 传上去，解压到 `/opt/auto-response`。

```bash
cd /opt/auto-response
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r server/requirements.txt -i https://mirrors.aliyun.com/pypi/simple
sudo cp deploy/.env.example deploy/.env
sudo nano deploy/.env        # 填第一步记下的值；生成 AGENT_TOKEN 见文件里的说明
sudo mkdir -p data && sudo chown -R bridge:bridge /opt/auto-response
sudo chmod 600 deploy/.env
```

### 2.3 启动服务

```bash
sudo cp deploy/auto-response.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now auto-response
sudo systemctl status auto-response
```

### 2.4 Nginx + HTTPS

```bash
sudo mkdir -p /etc/nginx/cert
# 把证书的 .pem 和 .key 上传到 /etc/nginx/cert/
sudo cp deploy/nginx.conf /etc/nginx/conf.d/auto-response.conf
sudo nano /etc/nginx/conf.d/auto-response.conf      # 把 bot.example.com 换成你的域名
sudo nginx -t && sudo systemctl reload nginx
curl https://bot.你的域名.com/healthz                  # 应返回 {"ok":true,...}
```

### 2.5 回企业微信保存回调

回到 1.2 的「接收消息」页面点**保存**，提示成功就说明加解密配置正确。

如果失败，查看日志：

```bash
sudo journalctl -u auto-response -f
```

---

## 第三步：家里 Windows 电脑

### 3.1 安装基础软件

1. **Git for Windows**：<https://git-scm.com/download/win>（Claude Code 在 Windows 上也需要它）
2. **Python 3.12**：<https://www.python.org/downloads/>，安装时勾选 *Add python.exe to PATH*
3. **Claude Code**：在 PowerShell 里运行下面的命令，然后执行 `claude`，用订阅账号登录一次

   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```

### 3.2 准备项目

- **建议给 Agent 单独 clone 一份项目**（比如放在 `D:\agent-projects\`），不要和你自己手动开发的目录混用。Agent 会在里面切换分支、合并代码
- 每个项目都要是 git 仓库，主分支干净
- 把依赖先装好（`npm install` 等），并确认测试/构建命令能跑通
- 在项目根目录写一份 `CLAUDE.md`，写上技术栈、目录结构、代码约定、怎么跑测试。Claude 每次都会读，这对效果影响很大
- 部署脚本：把你平时的部署步骤写成脚本（如 `scripts\deploy.ps1`），手动跑通

### 3.3 安装 Agent

```powershell
git clone https://github.com/LiTL0529/Auto-response.git D:\auto-response
```

```powershell
cd D:\auto-response\agent
```

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

```powershell
copy config.example.yaml config.yaml
```

```powershell
notepad config.yaml
```

`server_url` 填你的域名，`agent_token` 和服务器上的 `AGENT_TOKEN` 填同一个值，再配置你的项目。然后做一次自检：

```powershell
.venv\Scripts\python -m bridge_agent --config config.yaml --check
```

全部 `[OK]` 之后，先前台运行试试（双击 `run_agent.bat` 也可以），看到「已连接」就说明连上了：

```powershell
.venv\Scripts\python -m bridge_agent --config config.yaml
```

### 3.4 设置开机自启

```powershell
powershell -ExecutionPolicy Bypass -File install_windows.ps1
```

脚本会注册一个登录时启动、崩溃后自动重启的计划任务，并关闭插电时的自动睡眠。日志在 `agent\logs\agent.log`。

另外建议：

- **自动登录**：计划任务在登录 Windows 后才启动。电脑重启后要能自己登录：`Win+R` 运行 `netplwiz`，取消勾选「要使用本计算机，用户必须输入用户名和密码」
- **Windows 更新**：在「设置 → Windows 更新 → 高级选项 → 使用时段」里避开你上班的时间
- 如果系统环境变量里有 `ANTHROPIC_API_KEY`，Agent 默认会忽略它，强制使用订阅账号（`use_api_key: false`）

---

## 第四步：试一下

在企业微信「需求助手」里发：

1. `帮助`：应该收到用法说明
2. `进度`：最后一行显示「家里电脑：在线 ✅」
3. 一个小需求，比如「官网首页标题改成 xxx」：观察整个流程，最后会收到完成通知

## 常见问题

**收不到任何回复**
- 看服务器日志 `journalctl -u auto-response -f`
- 60020 错误：企业可信 IP 没配
- 签名校验失败：Token / EncodingAESKey / CorpID 填错了

**任务一直「排队中」**
- 家里电脑离线超过 10 分钟会主动提醒你。检查 Agent 日志，或者电脑是不是休眠了

**Claude 报额度用完**
- 订阅账号有使用上限，任务会标记失败并通知你。额度恢复后可以让朋友重新提，或者你直接回复对应需求

**Claude 想执行某个命令被拒绝**
- 默认只允许常见的构建/测试命令。可以在项目配置里加 `allowed_tools: ["Bash(make *)"]`

**想换成「完全不用确认就部署」**
- 不建议。如果确实需要，可以把部署步骤写进 `test` 或 `package` 命令里
