-- 014: 场景动作开关 (提交后行为按表单可配, 收集表脱离白名单)
-- surveys 增加 review_required + 三个动作开关。存量卷默认全开(1)保持白名单行为;
-- 已存在的收集表(category='collection')回填为全关(0): 免审、不加白、不发码、不进审核群队列。
-- 新库由模型 create_all 直接含这些列; 已有库执行本迁移。

ALTER TABLE surveys ADD COLUMN review_required BOOLEAN NOT NULL DEFAULT 1;
ALTER TABLE surveys ADD COLUMN action_add_whitelist BOOLEAN NOT NULL DEFAULT 1;
ALTER TABLE surveys ADD COLUMN action_issue_code BOOLEAN NOT NULL DEFAULT 1;
ALTER TABLE surveys ADD COLUMN action_notify_group BOOLEAN NOT NULL DEFAULT 1;

-- 已建的收集表回填为纯收集(免审 + 零白名单动作)
UPDATE surveys
SET review_required = 0,
    action_add_whitelist = 0,
    action_issue_code = 0,
    action_notify_group = 0
WHERE category = 'collection';
