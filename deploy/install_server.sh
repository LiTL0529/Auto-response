#!/usr/bin/env bash
# 一键部署 / 更新服务端（阿里云 ECS，支持 Ubuntu / Debian / Alibaba Cloud Linux / CentOS）。
#
# 在阿里云控制台 → ECS → 远程连接（Workbench）里以 root 运行：
#   git clone https://github.com/LiTL0529/Auto-response.git /opt/auto-response
#   bash /opt/auto-response/deploy/install_server.sh 你的域名
#
# 可以重复运行：会拉取最新代码、更新依赖、保留已有的 .env。
set -euo pipefail

DOMAIN="${1:-}"
APP=/opt/auto-response
VENV="$APP/.venv"
ENV_FILE="$APP/deploy/.env"
PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple}"
SKIP_CERT="${SKIP_CERT:-0}"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[注意] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$DOMAIN" ] || die "用法：bash install_server.sh 你的域名"
[ "$(id -u)" = 0 ] || die "请用 root 运行"
[ -d "$APP/server" ] || die "没找到 $APP/server，请先 git clone 仓库到 $APP"

# 宝塔面板：自带 nginx（/www/server/nginx）占用 80/443，且在 dnf.conf 里 exclude 了系统 nginx。
# 这种情况下不装系统 nginx、不申请证书，只给宝塔里同名站点加一个反向代理扩展配置（复用站点证书）。
BT_NGINX=/www/server/nginx/sbin/nginx
BT_VHOST=/www/server/panel/vhost/nginx
USE_BT=0
if [ -x "$BT_NGINX" ] && [ -d "$BT_VHOST" ]; then
    USE_BT=1
    echo "检测到宝塔面板，将使用宝塔的 nginx"
fi
SYS_PKGS="git curl"
[ "$USE_BT" = 1 ] || SYS_PKGS="nginx $SYS_PKGS"

# ---------------------------------------------------------------- 系统依赖
say "安装系统依赖"
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    # shellcheck disable=SC2086
    apt-get install -y -q python3 python3-venv python3-pip $SYS_PKGS
elif command -v dnf >/dev/null || command -v yum >/dev/null; then
    PM=$(command -v dnf || command -v yum)
    # shellcheck disable=SC2086
    "$PM" install -y $SYS_PKGS
    # Alibaba Cloud Linux 3 / CentOS 8 自带的 python3 是 3.6，装一个新版本
    for v in 3.12 3.11 3.9; do
        if "$PM" install -y "python$v" >/dev/null 2>&1 || "$PM" install -y "python${v/./}" >/dev/null 2>&1; then
            break
        fi
    done
else
    die "不支持的系统（没有 apt / dnf / yum）"
fi

PY=""
for c in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
        PY=$(command -v "$c"); break
    fi
done
[ -n "$PY" ] || die "需要 Python 3.9 及以上，自动安装失败，请手动安装后重试"
echo "使用 $PY（$("$PY" --version)）"

# ---------------------------------------------------------------- 代码与 Python 依赖
say "更新代码"
if [ -d "$APP/.git" ]; then
    git -C "$APP" pull --ff-only || warn "git pull 失败（网络问题？），继续使用当前代码"
fi

id bridge >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$APP" bridge

say "安装 Python 依赖"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q -U pip -i "$PIP_INDEX"
"$VENV/bin/pip" install -q -r "$APP/server/requirements.txt" -i "$PIP_INDEX"

# ---------------------------------------------------------------- .env
say "配置 .env"
mkdir -p "$APP/data"
if [ ! -f "$ENV_FILE" ]; then
    cp "$APP/deploy/.env.example" "$ENV_FILE"
    TOKEN=$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')
    sed -i \
        -e "s|^AGENT_TOKEN=.*|AGENT_TOKEN=$TOKEN|" \
        -e "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$DOMAIN|" \
        -e "s|^DATA_DIR=.*|DATA_DIR=$APP/data|" \
        -e "s|^WECOM_CORP_ID=ww0\+[[:space:]]|WECOM_CORP_ID= |" \
        -e "s|^DASHSCOPE_API_KEY=sk-x\+.*|DASHSCOPE_API_KEY=|" \
        "$ENV_FILE"
    echo "已生成 $ENV_FILE（AGENT_TOKEN 已随机生成）"
else
    echo "保留已有的 $ENV_FILE"
fi
# 去掉行尾注释（systemd 的 EnvironmentFile 不支持行尾 # 注释）
sed -i -E 's/^([A-Z_]+=[^#]*[^[:space:]#])[[:space:]]+#.*$/\1/; s/^([A-Z_]+=)[[:space:]]+#.*$/\1/' "$ENV_FILE"
chown -R bridge:bridge "$APP/data"
chown root:bridge "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ---------------------------------------------------------------- systemd
say "配置 systemd 服务"
cp "$APP/deploy/auto-response.service" /etc/systemd/system/auto-response.service
systemctl daemon-reload
systemctl enable auto-response >/dev/null 2>&1 || true

missing=$(grep -E '^(WECOM_CORP_ID|WECOM_AGENT_ID|WECOM_APP_SECRET|WECOM_CALLBACK_TOKEN|WECOM_ENCODING_AES_KEY|OWNER_USERID)=$' "$ENV_FILE" | cut -d= -f1 | tr '\n' ' ' || true)
if [ -z "$missing" ]; then
    systemctl restart auto-response
    sleep 2
    systemctl is-active --quiet auto-response && echo "服务已启动" || warn "服务启动失败：journalctl -u auto-response -n 50"
else
    warn "企业微信配置还没填：$missing"
    warn "填好后运行：systemctl restart auto-response"
fi

# ---------------------------------------------------------------- Nginx（宝塔）
if [ "$USE_BT" = 1 ]; then
    say "配置宝塔 nginx"
    BT_SITE="$BT_VHOST/$DOMAIN.conf"
    BT_EXT="$BT_VHOST/extension/$DOMAIN"
    if [ ! -f "$BT_SITE" ]; then
        die "宝塔里还没有站点 $DOMAIN：请在宝塔面板 → 网站 → 添加站点 $DOMAIN，并在 SSL 里申请证书后重新运行本脚本"
    fi
    grep -q 'listen 443' "$BT_SITE" || warn "宝塔站点 $DOMAIN 还没开 HTTPS（企业微信回调需要 https），请在宝塔面板 → 网站 → SSL 申请证书"
    grep -q "extension/$DOMAIN/" "$BT_SITE" || warn "$BT_SITE 里没有 include extension 目录，下面的配置不会生效"
    mkdir -p "$BT_EXT"
    # 用 ^~ / = 匹配，避免宝塔站点里的「敏感文件」正则 location（如 *.log、requirements.txt）拦截下载链接
    cat > "$BT_EXT/auto-response.conf" <<'EOF'
# Auto-response（企业微信需求助手），由 /opt/auto-response/deploy/install_server.sh 生成
# 程序在 /opt/auto-response，systemd 服务 auto-response，只监听 127.0.0.1:8000
location ^~ /wecom/     { include /www/server/panel/vhost/nginx/extension/auto-response-proxy.inc; }
location ^~ /api/agent/ { include /www/server/panel/vhost/nginx/extension/auto-response-proxy.inc; }
location ^~ /files/     { include /www/server/panel/vhost/nginx/extension/auto-response-proxy.inc; }
location = /healthz     { include /www/server/panel/vhost/nginx/extension/auto-response-proxy.inc; }
EOF
    cat > "$BT_VHOST/extension/auto-response-proxy.inc" <<'EOF'
proxy_pass http://127.0.0.1:8000;
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header Connection "";
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_request_buffering off;
proxy_buffering off;
proxy_read_timeout 120s;
client_max_body_size 2g;
add_header X-Robots-Tag "noindex" always;
EOF
    if "$BT_NGINX" -t; then
        "$BT_NGINX" -s reload
        echo "已写入 $BT_EXT/auto-response.conf 并重载宝塔 nginx"
    else
        rm -f "$BT_EXT/auto-response.conf"
        die "宝塔 nginx 配置检查失败，已撤销 auto-response.conf"
    fi
fi

# ---------------------------------------------------------------- Nginx（系统）
if [ "$USE_BT" = 0 ]; then
say "配置 Nginx"
CONF=/etc/nginx/conf.d/auto-response.conf
CERT_DIR=/etc/letsencrypt/live/$DOMAIN
PROXY='
    client_max_body_size 2g;
    proxy_request_buffering off;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }'

write_http_conf() {
    cat > "$CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
$PROXY
}
EOF
}

write_https_conf() {
    cat > "$CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
$PROXY
}
EOF
}

mkdir -p /var/www/acme
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
if [ -f "$CERT_DIR/fullchain.pem" ]; then write_https_conf; else write_http_conf; fi
nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx

# ---------------------------------------------------------------- HTTPS 证书（Let's Encrypt）
if [ "$SKIP_CERT" != 1 ] && [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    say "申请 HTTPS 证书（Let's Encrypt）"
    [ -x /opt/certbot/bin/certbot ] || { "$PY" -m venv /opt/certbot && /opt/certbot/bin/pip install -q certbot -i "$PIP_INDEX"; }
    if /opt/certbot/bin/certbot certonly --webroot -w /var/www/acme -d "$DOMAIN" \
        --non-interactive --agree-tos --register-unsafely-without-email; then
        write_https_conf
        nginx -t && systemctl reload nginx
        cat > /etc/cron.d/auto-response-certbot <<EOF
17 3 * * * root /opt/certbot/bin/certbot renew -q --deploy-hook "systemctl reload nginx"
EOF
        echo "证书申请成功，已开启 HTTPS 并设置自动续期"
    else
        warn "证书申请失败。常见原因："
        warn "  1) 域名 $DOMAIN 还没解析到本机公网 IP（阿里云 云解析DNS 添加 A 记录）"
        warn "  2) 安全组没有放行 80 / 443 端口"
        warn "处理后重新运行本脚本即可"
    fi
fi
fi  # USE_BT = 0

# ---------------------------------------------------------------- 检查
say "检查"
sleep 1
if curl -fsS -m 5 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "本机服务：正常"
else
    echo "本机服务：未运行（企业微信配置未填写，或查看 journalctl -u auto-response -n 50）"
fi

AGENT_TOKEN=$(grep '^AGENT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
cat <<EOF

================================================================
部署完成。接下来：

1. 企业微信「接收消息」回调 URL 填：
     https://$DOMAIN/wecom/callback

2. 把企业微信的各项值填进配置文件：
     nano $ENV_FILE
   然后重启服务：
     systemctl restart auto-response

3. 家里电脑 agent/config.yaml 里填：
     server_url: https://$DOMAIN
     agent_token: $AGENT_TOKEN

4. 企业微信「企业可信IP」填本机公网 IP：
     $(curl -fsS -m 5 https://ifconfig.me 2>/dev/null || echo '在 ECS 控制台查看')
================================================================
EOF
