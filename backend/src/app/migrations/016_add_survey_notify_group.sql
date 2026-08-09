-- 016: 按问卷指定通知投递目标群
-- surveys.notify_group_id: NULL = 沿用插件默认审核群 + 玩家向语义 (@ 提交者、需其在群、回填
-- in_review_group); 非 NULL = 管理向语义 (纯文本播报到该群, QQ 号写进正文, 不 @ 不查成员)。
-- 存量卷一律 NULL, 白名单卷行为零变化。
-- 新库由模型 create_all 直接含该列; 已有库执行本迁移。

ALTER TABLE surveys ADD COLUMN notify_group_id INTEGER;
