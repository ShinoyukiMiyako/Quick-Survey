# Quick-Survey 部署指南

## 📋 部署步骤概览

1. 本地构建前端和打包后端
2. 上传到服务器
3. 配置后端服务 (systemd)
4. 配置前端 (Nginx)

---

## 🏠 本地操作

### 1. 构建前端

```bash
cd frontend
pnpm install
pnpm build
# 构建产物在 frontend/dist 目录
```

### 2. 打包后端

```bash
# 运行打包脚本
./deploy/pack.sh
# 或者手动打包
cd backend
tar -czvf ../deploy/backend.tar.gz \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.venv' \
    --exclude='data/*.db' \
    .
```

### 3. 上传到服务器

```bash
# 上传后端包
scp deploy/backend.tar.gz user@your-server:/opt/quick-survey/

# 上传前端构建产物
scp -r frontend/dist/* user@your-server:/var/www/quick-survey/
```

---

## 🖥️ 服务器操作

### 1. 安装依赖

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip nginx

# CentOS/RHEL
sudo dnf install python3.11 python3.11-venv nginx
```

### 2. 部署后端

```bash
# 创建目录
sudo mkdir -p /opt/quick-survey
cd /opt/quick-survey

# 解压后端
tar -xzvf backend.tar.gz

# 创建虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 创建数据目录
mkdir -p data uploads

# 修改配置文件
cp config.example.yml config.yml
nano config.yml  # 编辑配置
```

### 3. 配置 systemd 服务

```bash
# 复制服务文件
sudo cp /opt/quick-survey/deploy/quick-survey.service /etc/systemd/system/

# 重载配置
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start quick-survey

# 设置开机自启
sudo systemctl enable quick-survey

# 查看状态
sudo systemctl status quick-survey

# 查看日志
sudo journalctl -u quick-survey -f
```

### 4. 配置 Nginx

```bash
# 复制配置文件
sudo cp /opt/quick-survey/deploy/nginx.conf /etc/nginx/sites-available/quick-survey

# 启用站点
sudo ln -s /etc/nginx/sites-available/quick-survey /etc/nginx/sites-enabled/

# 测试配置
sudo nginx -t

# 重载 Nginx
sudo systemctl reload nginx
```

---

## 🔧 常用命令

### 服务管理

```bash
# 启动/停止/重启
sudo systemctl start quick-survey
sudo systemctl stop quick-survey
sudo systemctl restart quick-survey

# 查看状态
sudo systemctl status quick-survey

# 查看日志
sudo journalctl -u quick-survey -f
sudo journalctl -u quick-survey --since "1 hour ago"
```

### 更新部署

```bash
# 更新后端
cd /opt/quick-survey
sudo systemctl stop quick-survey
tar -xzvf backend.tar.gz
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl start quick-survey

# 更新前端
sudo cp -r /path/to/dist/* /var/www/quick-survey/
```

### 数据库迁移 (更新后端时必做)

`init_db()` 只做 `create_all`, 它建缺失的表, **不会给已有表加列**。所以凡是本次更新带了
`src/app/migrations/` 下的新编号 SQL, 就必须在重启服务前手动执行一次; 漏跑的表现是服务
"启动成功"但所有涉及该表的接口全部 500 (SQLAlchemy 查询里带了库里不存在的列)。

顺序固定为: 先快照, 再迁移, 最后重启。

```bash
cd /opt/quick-survey/backend

# 1) 一致性快照。直接 cp survey.db 会漏掉 WAL 里尚未 checkpoint 的数据,
#    必须走 sqlite3 的 backup API 才能拿到完整快照
.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect("data/survey.db")
dst = sqlite3.connect("data/survey.db.snapshot")
src.backup(dst)
dst.close(); src.close()
PY

# 2) 幂等执行迁移。SQLite 不支持 ADD COLUMN IF NOT EXISTS,
#    故先查 PRAGMA table_info 判断该迁移是否已应用
.venv/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect("data/survey.db")
cols = [r[1] for r in db.execute("PRAGMA table_info(surveys)")]
if "starts_at" not in cols:          # 换成本次迁移新增的任一列名
    db.executescript(open("src/app/migrations/015_add_survey_lifecycle_access.sql").read())
    db.commit()
    print("migration applied")
else:
    print("migration skipped (already applied)")
db.close()
PY

# 3) 重启并冒烟
systemctl restart quick-survey
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/api/v1/public/surveys
```

已有迁移与其判别列:

| 迁移 | 判别列 (存在即已应用) |
|---|---|
| `013_add_survey_portal_fields.sql` | `surveys.sort_order` |
| `014_add_survey_actions.sql` | `surveys.review_required` |
| `015_add_survey_lifecycle_access.sql` | `surveys.starts_at` |

`migrate_condition_depends_on_to_id.py` 与 `migrate_player_name_nullable.py` 是一次性数据
订正脚本, 自带 `--apply` 开关与幂等判断, 只在首次升级到对应版本时跑。

---

## ⚙️ 配置说明

### 后端配置 (config.yml)

```yaml
server:
  host: "127.0.0.1"  # 生产环境只监听本地，由 Nginx 代理
  port: 8000
  debug: false        # 生产环境关闭 debug

cors:
  allowed_origins: ["https://your-domain.com"]  # 设置实际域名
```

### Nginx 配置

- 前端静态文件目录: `/var/www/quick-survey/`
- 后端 API 代理: `/api/` -> `http://127.0.0.1:8000`
- 上传文件代理: `/uploads/` -> `http://127.0.0.1:8000/uploads/`

---

## Turnstile siteverify 中转

问卷后端所在的阿里云广州机房到 `challenges.cloudflare.com` 的 TLS 握手会被中途阻断
(2026-07-19 起出现, 07-26 实测失败率 85%), 导致真人用户提交被 500 拒绝。
同账号的深圳机到同一端点完全通畅, 故由其转发。

部署方式见 `deploy/nginx-turnstile-relay.conf` 文件头注释, 要点:

1. 把该文件里的 `location` 块粘进**中转机**上一个已有 HTTPS 站点的 server 块
   (当前挂在 `panel.mcwok.cn` 的 443 下), `nginx -t` 通过后 `systemctl reload nginx`
2. 问卷后端 `config.yml` 设 `security.turnstile.verify_url` 指向该地址
3. 中转对来源 IP 做了 `allow` 白名单, 换问卷机公网 IP 时必须同步改

相关配置项 (`config.yml` 的 `security.turnstile`):

| 配置项 | 说明 |
|---|---|
| `verify_url` | siteverify 端点。线路通畅的机房保持 Cloudflare 官方地址直连即可 |
| `fail_open` | siteverify 不可达时是否放行。关掉则网络一断全员提交失败; 打开则降级放行并记 WARNING, 由 IP 限流/耗时检测/人工审核兜底 |

统计降级放行次数:

```bash
journalctl -u quick-survey --since "7 days ago" | grep -c "降级放行"
```

---

## 🔒 安全建议

1. **防火墙**: 只开放 80/443 端口，后端 8000 端口只允许本地访问
2. **HTTPS**: 使用 Let's Encrypt 配置 SSL 证书
3. **权限**: 使用非 root 用户运行服务
4. **配置**: 确保 `config.yml` 文件权限为 600

```bash
# 配置 Let's Encrypt
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```
