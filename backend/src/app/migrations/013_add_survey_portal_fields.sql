-- 013: 多表单门户编排 + 展示字段
-- surveys 增加 排序/置顶/分栏/可见性/生命周期 与 卡片展示字段, 支撑"入口可选 + 可排序 + 分栏"。
-- 新库由模型 create_all 直接含这些列; 已有库执行本迁移。
-- SQLite ADD COLUMN 带 DEFAULT, 存量行自动取默认值。

ALTER TABLE surveys ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
ALTER TABLE surveys ADD COLUMN is_pinned BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE surveys ADD COLUMN category VARCHAR(32) NOT NULL DEFAULT 'whitelist';
ALTER TABLE surveys ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'public';
ALTER TABLE surveys ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'published';
ALTER TABLE surveys ADD COLUMN cover_url VARCHAR(512);
ALTER TABLE surveys ADD COLUMN icon VARCHAR(64);
ALTER TABLE surveys ADD COLUMN theme_color VARCHAR(16);
ALTER TABLE surveys ADD COLUMN summary VARCHAR(255);
ALTER TABLE surveys ADD COLUMN estimated_minutes INTEGER;

-- 存量卷按 created_at 赋予稳定的初始排序位 (0..n-1), 便于面板拖拽微调
UPDATE surveys SET sort_order = (
    SELECT COUNT(*) FROM surveys s2
    WHERE s2.created_at < surveys.created_at
       OR (s2.created_at = surveys.created_at AND s2.id < surveys.id)
);
