# Quick-Survey

问卷调查系统 - 用于玩家白名单审核的问卷平台

## 功能特性

- 📝 **问卷管理**：创建、编辑、删除问卷模板
- 🎲 **随机题库**：支持从题库中随机抽取题目展示
- 📸 **图片上传**：支持玩家上传截图作为答案
- ✅ **多种题型**：单选、多选、判断、简答、图片上传
- 🔍 **审核系统**：管理员查看提交列表、审核通过/拒绝
- 🔐 **JWT 认证**：与 ConvenientAccess 共享认证，管理员无缝访问

## 技术栈

- **后端框架**: FastAPI
- **数据库**: SQLite + SQLAlchemy (异步)
- **认证**: JWT (与 Java 端共享)
- **文件存储**: 本地文件系统

## 快速开始

### 1. 安装依赖

```bash
cd Quick-Survey

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -e .
```

### 2. 配置

```bash
# 复制配置文件
cp config.example.yml config.yml

# 编辑配置，设置 admin_password（从 Java 端 config.yml 复制）
```

**重要**: `auth.admin_password` 必须与 Java 端 `api.auth.admin-password` 保持一致！

### 3. 运行

```bash
# 开发模式
python run.py

# 或使用 uvicorn
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000/docs 查看 API 文档

## API 概览

### 管理员接口（需要 JWT 认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/surveys` | 创建问卷 |
| GET | `/api/v1/surveys` | 获取问卷列表 |
| GET | `/api/v1/surveys/{id}` | 获取问卷详情 |
| PATCH | `/api/v1/surveys/{id}` | 更新问卷 |
| DELETE | `/api/v1/surveys/{id}` | 删除问卷 |
| POST | `/api/v1/surveys/{id}/questions` | 添加问题 |
| PATCH | `/api/v1/surveys/{id}/questions/{qid}` | 更新问题 |
| DELETE | `/api/v1/surveys/{id}/questions/{qid}` | 删除问题 |
| GET | `/api/v1/submissions` | 获取提交列表 |
| GET | `/api/v1/submissions/{id}` | 获取提交详情 |
| PATCH | `/api/v1/submissions/{id}/review` | 审核提交 |
| GET | `/api/v1/submissions/stats/overview` | 获取统计概览 |

### 公开接口（无需认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/public/surveys/{code}` | 获取问卷（通过短码） |
| POST | `/api/v1/public/surveys/{code}/submit` | 提交问卷 |
| POST | `/api/v1/public/upload` | 上传图片 |
| POST | `/api/v1/public/upload/file` | 上传文件题附件 |

## 题型说明

题型的唯一事实源是 `backend/src/app/services/question_types.py` 的注册表，新增题型只在那里登记一次。

| 类型 | 说明 | 答案格式 |
|------|------|----------|
| `single` | 单选题 | `{"value": "A"}` |
| `select` | 下拉单选 | `{"value": "A"}` |
| `multiple` | 多选题 | `{"values": ["A", "B"]}` |
| `boolean` | 判断题 | `{"value": true}` |
| `text` | 多行文本 | `{"text": "答案内容"}` |
| `short_text` | 单行文本 | `{"text": "答案内容"}` |
| `number` | 数字题 | `{"value": 42}` |
| `date` | 日期题 | `{"value": "2026-09-06"}` |
| `rating` | 评分题 | `{"value": 5}` |
| `image` | 图片上传 | `{"images": ["/uploads/xxx.jpg"]}` |
| `file` | 文件上传 | `{"files": [{"url": "/uploads/xxx.ysm", "name": "模型.ysm", "size": 1024}]}` |
| `section` | 分节说明（不收答案） | 无 |

文件题按扩展名白名单把关（`upload.allowed_file_extensions`），单题可用 `validation.allowed_extensions`
进一步收窄；体积上限走 `upload.max_file_size_mb`，与图片的 `max_size_mb` 分开配。

## 目录结构

```
Quick-Survey/
├── config.example.yml    # 配置文件示例
├── pyproject.toml        # 项目配置
├── run.py                # 启动脚本
├── QuickSurvey_API.postman_collection.json  # Postman API 集合
├── src/
│   └── app/
│       ├── main.py       # FastAPI 应用入口
│       ├── api/          # API 路由
│       │   ├── surveys.py      # 问卷管理
│       │   ├── submissions.py  # 提交管理
│       │   └── public.py       # 公开接口
│       ├── core/         # 核心模块
│       │   ├── config.py       # 配置管理
│       │   ├── jwt.py          # JWT 验证
│       │   └── deps.py         # 依赖注入
│       ├── db/           # 数据库
│       │   └── database.py     # 数据库连接
│       ├── models/       # 数据模型
│       │   └── models.py       # SQLAlchemy 模型
│       ├── schemas/      # Pydantic 模式
│       │   └── schemas.py      # 请求/响应模式
│       └── services/     # 业务逻辑
│           ├── survey.py       # 问卷服务
│           └── file.py         # 文件服务
├── data/                 # 数据库文件
└── uploads/              # 上传文件
```

## 认证说明

本系统与 ConvenientAccess Java 插件共享 JWT 认证：

1. 管理员通过 Java 端 `/api/v1/admin/login` 登录获取 JWT Token
2. 使用该 Token 访问 Quick-Survey 的管理员接口
3. Token 验证逻辑与 Java 端完全一致

## License

MIT
