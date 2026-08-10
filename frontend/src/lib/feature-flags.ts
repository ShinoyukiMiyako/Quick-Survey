// 前端功能开关: 集中管理"临时停用但随时要开回来"的流程, 避免大段注释散落各处。

/**
 * 注册码流程 (审核通过 -> 页面领码 -> 游戏内 /register <密码> <确认> <注册码>).
 *
 * 2026-08-10 临时关闭: 玩家普遍看不懂"先领码再带码注册", 卡在进服第一步。
 * 关闭期间 mod 端的 /register 也不再校验码 (Convenient-access PlayerAuthService.register),
 * 玩家过审加白后直接进服, 首次进服用 /register <密码> <确认密码> 自助设密。
 *
 * 关闭只影响前端入口与文案: 后端 /api/public/submissions/{token}/code 与 mod 的发码端点都还在,
 * 管理员仍可手工补发。恢复时把本值改回 true, 并同步恢复 mod 端的校验块与 QQ 机器人过审文案。
 */
// 显式标注 boolean: 避免 TS 收窄成字面量类型 false 后, 把 true 分支的 JSX 判成不可达而报类型错
export const REGISTRATION_CODE_ENABLED: boolean = false
