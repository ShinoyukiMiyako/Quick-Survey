// 问题选项
export interface QuestionOption {
  value: string
  label: string
}

// 问题验证规则
export interface QuestionValidation {
  min_length?: number
  max_length?: number
  max_images?: number
  min_value?: number   // number 题下限（闭区间）
  max_value?: number   // number 题上限（闭区间）
  max_rating?: number  // rating 题满分，缺省 5
  min_date?: string    // date 题下限，"YYYY-MM-DD"
  max_date?: string    // date 题上限，"YYYY-MM-DD"
}

// 条件运算符（与后端 services/conditions.py 一一对应）
export type ConditionOperator =
  | 'eq'
  | 'neq'
  | 'in'
  | 'not_in'
  | 'contains'
  | 'gt'
  | 'lt'
  | 'answered'
  | 'not_answered'

// 单条条件规则
export interface ConditionRule {
  question_id: number  // 依赖题的 question_id（稳定引用，不随题序/编辑变化）
  operator: ConditionOperator
  value?: string | number | string[] // answered / not_answered 忽略此字段
}

// 条件显示规则
// 用于实现分支逻辑：根据其它题的答案决定是否显示当前题目。
// 新旧两套字段并存：存量库里全是旧形态，归一化交给 lib/conditions.ts。
export interface QuestionCondition {
  // 新形态
  action?: 'show' | 'hide'  // 缺省 show
  match?: 'all' | 'any'     // 缺省 all
  rules?: ConditionRule[]
  // 旧形态（存量数据）
  depends_on?: number
  show_when?: string | string[]
}

// 问题类型
export type QuestionType =
  | 'single'
  | 'select'
  | 'multiple'
  | 'boolean'
  | 'text'
  | 'short_text'
  | 'number'
  | 'date'
  | 'rating'
  | 'image'

// 问卷可填状态（后端 compute_availability 结果）
export type AvailabilityState = 'open' | 'inactive' | 'unpublished' | 'not_started' | 'ended' | 'full'

export interface Availability {
  state: AvailabilityState
  message: string | null // open 时为 null，其余为可展示给玩家的中文文案
}

// 问题
export interface Question {
  id: number
  title: string
  description?: string
  type: QuestionType
  options?: QuestionOption[]
  is_required: boolean
  validation?: QuestionValidation
  condition?: QuestionCondition  // 条件显示规则
  role?: 'player_name' | 'qq'    // 语义标记: 系统字段题 (玩家名/QQ), 后端据此抽取结构化字段
}

// 公开问卷响应
export interface PublicSurvey {
  code: string
  title: string
  description?: string
  category?: string          // whitelist / collection
  requires_review?: boolean  // 是否需人工审核 (收集表为 false)
  issues_code?: boolean      // 通过后是否发注册码 (收集表为 false)
  availability: Availability
  locked: boolean            // 设了访问口令; 为 true 时后端不下发 questions
  require_consent: boolean
  privacy_notice: string | null
  theme_color: string | null
  icon: string | null
  success_message: string | null
  estimated_minutes: number | null
  questions: Question[]      // state 非 open 或 locked 时为空数组 (不泄题)
}

// 门户入口列表项 (轻量, 不含 questions; 可空字段随后端 JSON 为 null)
export interface SurveyListItem {
  code: string
  title: string
  description: string | null
  summary: string | null
  category: string
  cover_url: string | null
  icon: string | null
  theme_color: string | null
  estimated_minutes: number | null
  is_pinned: boolean
  sort_order: number
  question_count: number
  availability: Availability // 不可填的卷仍会列出, 由前端渲染徽标并禁点
  locked: boolean
}

// 答案提交
export interface AnswerSubmit {
  question_id: number
  content: {
    value?: string | boolean | number // 单选、下拉、判断、数字、日期、评分
    values?: string[] // 多选
    text?: string // 简答、单行文本
    images?: string[] // 图片上传
  }
}

// 提交请求
export interface SubmissionCreate {
  player_name?: string  // 可空: 配置了 role=player_name 题时由后端从答案抽取
  answers: AnswerSubmit[]
  // 安全相关字段
  turnstile_token?: string
  start_time?: number
  // 门禁字段
  access_password?: string // 口令卷提交时必带, 后端二次校验防绕过 unlock
  consent?: boolean        // require_consent 的卷必须为 true
}

// 安全配置
export interface SecurityConfig {
  turnstile_enabled: boolean
  time_check_enabled: boolean
  min_submit_time: number
}

// API 响应
export interface ApiResponse<T = unknown> {
  success: boolean
  data?: T
  error?: {
    code: string
    message: string
  }
}

// 上传响应
export interface UploadResponse {
  filename: string
  stored_name: string
  url: string
  size: number
  mime_type: string
}

// 提交状态查询 - 时间线
export interface SubmissionTimeline {
  submitted_at: string | null
  first_viewed_at: string | null
  reviewed_at: string | null
}

// 提交状态查询 - 单条记录
export interface SubmissionStatus {
  id: number
  token: string
  player_name: string
  status: 'pending' | 'approved' | 'rejected'
  status_text: string
  timeline: SubmissionTimeline
  fill_duration: string | null
  review_note: string | null
  survey_title: string | null
  code_issued: boolean       // 是否已领取过注册码
  can_get_code: boolean      // 是否可领取 (已通过且未领取)
}

// 提交成功响应
export interface SubmitResult {
  id: number
  token: string              // 自助凭据: 玩家凭此查询进度并领取注册码
  message: string
}

// 领取注册码响应: 成功带明文码, 已领取则 already_issued=true
export interface RegistrationCodeResult {
  registration_code?: string
  code_expires_minutes?: number
  already_issued?: boolean
  message?: string
}
