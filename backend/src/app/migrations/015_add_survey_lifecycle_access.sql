-- 015: 问卷生命周期 / 提交配额 / 访问口令 / 合规声明 / webhook 推送
-- surveys 增加: 开放窗口(starts_at/ends_at)、提交上限(总量与每 IP)、访问口令哈希、
-- 同意声明开关与正文、不可填与提交成功的自定义文案、webhook 推送开关与地址。
-- 这些能力此前只能靠 is_active 手动开关近似, 无法表达"到点自动截止 / 满额自动停收 / 凭口令填"。
--
-- 为何不需要整表重建: 新列全部可空或带 DEFAULT, 且不涉及改类型/加约束/改主键,
-- SQLite 的 ALTER TABLE ADD COLUMN 可直接原地追加, 无需 create-copy-drop-rename 那套重建流程;
-- 存量行自动取默认值, 布尔列默认 0 表示关闭, 现有问卷行为完全不变。
-- 布尔列用 INTEGER: SQLite 无原生布尔类型, INTEGER 亲和性最贴近 0/1 的实际存储。
-- SQLite 不支持 ADD COLUMN IF NOT EXISTS, 幂等由部署脚本先查 PRAGMA table_info 决定是否执行。
-- 新库由模型 create_all 直接含这些列; 已有库执行本迁移。

ALTER TABLE surveys ADD COLUMN action_webhook INTEGER NOT NULL DEFAULT 0;
ALTER TABLE surveys ADD COLUMN webhook_url VARCHAR(512);
ALTER TABLE surveys ADD COLUMN starts_at DATETIME;
ALTER TABLE surveys ADD COLUMN ends_at DATETIME;
ALTER TABLE surveys ADD COLUMN max_submissions INTEGER;
ALTER TABLE surveys ADD COLUMN max_submissions_per_ip INTEGER;
ALTER TABLE surveys ADD COLUMN access_password_hash VARCHAR(255);
ALTER TABLE surveys ADD COLUMN require_consent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE surveys ADD COLUMN privacy_notice TEXT;
ALTER TABLE surveys ADD COLUMN closed_message VARCHAR(500);
ALTER TABLE surveys ADD COLUMN success_message VARCHAR(500);
