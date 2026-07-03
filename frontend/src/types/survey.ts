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
}

// 条件显示规则
// 用于实现分支逻辑：根据某道题的答案决定是否显示当前题目
export interface QuestionCondition {
  depends_on: number           // 依赖题的 question_id（稳定引用，不随题序/编辑变化）
  show_when: string | string[] // 触发显示的答案值（支持单值或多值）
}

// 问题类型
export type QuestionType = 'single' | 'multiple' | 'boolean' | 'text' | 'image'

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
  questions: Question[]
}

// 答案提交
export interface AnswerSubmit {
  question_id: number
  content: {
    value?: string | boolean // 单选、判断
    values?: string[] // 多选
    text?: string // 简答
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
