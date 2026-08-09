import type { AnswerSubmit, Question, QuestionType, QuestionValidation } from '@/types/survey'

// 答案内容校验: 与后端 app/services/question_types.py 的 validate_answer 逐条同源。
// 后端只在提交那一刻校验, 玩家要答完整卷、过完 Turnstile 才被 400 打回; 前端同源再算一次,
// 在离开当前题时就把原因说清楚。两端规则一旦漂移就会出现"前端放行、后端 400"的死局,
// 改这里必须同步改 question_types.py, 反之亦然。

type AnswerContent = AnswerSubmit['content']
type ContentKey = keyof AnswerContent

// 各题型的答案主键, 对应后端 QUESTION_TYPES 的 content_key。
// Partial: 分节说明块不收答案, 没有主键可言 —— 查不到主键的题型在 validateAnswer 开头就放行,
// 与后端 `spec is None or not spec.answerable` 的提前返回对齐。
const CONTENT_KEY: Partial<Record<QuestionType, ContentKey>> = {
  single: 'value',
  select: 'value',
  multiple: 'values',
  boolean: 'value',
  text: 'text',
  short_text: 'text',
  number: 'value',
  date: 'value',
  rating: 'value',
  image: 'images',
}

// 主键取不到时的回退键, 对齐后端 _CONTENT_FALLBACK: 存量库里 text 题写成 {"value": "..."}
// 的历史扁平数据还在, 只认主键会把老答案当成未填而整片跳过校验
const CONTENT_FALLBACK: Record<ContentKey, ContentKey[]> = {
  value: ['value', 'text'],
  text: ['text', 'value'],
  values: ['values', 'value'],
  images: ['images', 'value'],
}

const LIST_CONTENT_KEYS: ContentKey[] = ['values', 'images']

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/
const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

/** 按答案主键取出原始值 (不做任何归一化), 取不到返回 null。 */
function rawValue(contentKey: ContentKey, content?: AnswerContent | null): unknown {
  if (!content) return null
  const keys = CONTENT_FALLBACK[contentKey]
  const expectsList = LIST_CONTENT_KEYS.includes(contentKey)
  for (let i = 0; i < keys.length; i += 1) {
    const value = content[keys[i]]
    if (value === null || value === undefined) continue
    // 回退键只有形态对得上才采信: 多选/图片回退到 value 时必须仍是数组, 否则宁可当未作答,
    // 免得把一个标量喂进列表分支
    if (i > 0 && expectsList && !Array.isArray(value)) continue
    return value
  }
  return null
}

/** 标量/数组是否算"填了东西"。boolean 与数字 (含 false / 0) 一律算填了。 */
function isFilled(value: unknown): boolean {
  if (value === null || value === undefined) return false
  if (typeof value === 'boolean' || typeof value === 'number') return true
  if (typeof value === 'string') return value.trim() !== ''
  if (Array.isArray(value)) return value.some((item) => isFilled(item))
  return true
}

/**
 * 标量归一化为可比较的字符串。
 * 后端 _fmt_num 要把 3.0 抹成 "3" 再比, JS 的 String(3) 本就是 "3", 不必另做归一。
 */
function scalarText(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return String(value)
  return String(value).trim()
}

/**
 * validation JSON 里的数字可能是 null / 空串 / 字符串数字, 取不到就视为未配置该限制。
 * boolean 单独排除: 后端那边 bool 是 int 子类, float(True) 会静默变成 1.0。
 */
function asFloat(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === 'boolean') return null
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string') return null
  const text = value.trim()
  // 与 Python float("") 抛 ValueError 对齐, 不能沿用 JS 的 Number("") === 0
  if (text === '') return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? parsed : null
}

/** 只接受"整数值": 4 / 4.0 / "4" 可以, 3.5 / "abc" 不行。 */
function asInt(value: unknown): number | null {
  const parsed = asFloat(value)
  if (parsed === null || !Number.isInteger(parsed)) return null
  return parsed
}

/**
 * 严格 YYYY-MM-DD 解析, 不合法返回 null; 合法时原样返回该字符串。
 * 定长零填充的 ISO 日期字典序即时间序, 直接用字符串比大小, 不必再造 Date 对象;
 * 闰年自己算而不走 Date.UTC —— 后者会把两位数年份映射到 1900+, 与后端的公历判定错位。
 */
function parseIsoDate(value: unknown): string | null {
  if (typeof value !== 'string' || !DATE_PATTERN.test(value)) return null
  const year = Number(value.slice(0, 4))
  const month = Number(value.slice(5, 7))
  const day = Number(value.slice(8, 10))
  // 后端 date 的年份下限是 1, "0000-01-01" 在那边同样抛 ValueError
  if (year < 1 || month < 1 || month > 12) return null
  const isLeap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0
  const maxDay = month === 2 && isLeap ? 29 : DAYS_IN_MONTH[month - 1]
  if (day < 1 || day > maxDay) return null
  return value
}

/** 选项的合法取值集合; 兼容历史里直接存字符串数组的写法, 与后端 _option_entries 一致。 */
function allowedOptionValues(options?: readonly unknown[] | null): Set<string> {
  const allowed = new Set<string>()
  for (const option of options ?? []) {
    if (typeof option === 'string') {
      allowed.add(option)
      continue
    }
    if (!option || typeof option !== 'object') continue
    const value = (option as { value?: unknown }).value
    if (value === null || value === undefined) continue
    allowed.add(String(value))
  }
  return allowed
}

// 不收答案的展示型题块, 对齐后端 QUESTION_TYPES 里 answerable=false 的项。
// 刻意用排除法而不是"查得到 CONTENT_KEY 才算收答案": 认不出的题型后端按收答案处理,
// 这里必须一致, 否则存量脏数据的题会在前端被悄悄跳过必填校验, 到后端才被 400 打回。
const UNANSWERABLE_TYPES = new Set<QuestionType>(['section'])

/** 该题是否收答案。分节说明块只渲染文字, 不参与必填判定、题号与答案收集。 */
export function isAnswerableQuestion(question: Question): boolean {
  return !UNANSWERABLE_TYPES.has(question.type)
}

/**
 * 校验一条"已填内容"是否合法, 返回可直接展示给玩家的中文原因, 合法返回 null。
 * 必填与否不在这里判定: 内容为空一律放行, 调用方按 isAnswered + is_required 自行决定。
 */
export function validateAnswer(question: Question, content?: AnswerContent | null): string | null {
  const contentKey: ContentKey | undefined = CONTENT_KEY[question.type]
  // 未知题型 (存量脏数据) 放行, 对齐后端 spec is None 的提前返回
  if (!contentKey) return null

  const raw = rawValue(contentKey, content)
  if (!isFilled(raw)) return null

  const rules: QuestionValidation = question.validation ?? {}

  switch (question.type) {
    case 'single':
    case 'select': {
      const allowed = allowedOptionValues(question.options)
      // 题目没配选项时无从校验, 放行, 交由管理端去修题
      if (allowed.size > 0 && !allowed.has(scalarText(raw))) return '所选选项不在可选范围内'
      return null
    }

    case 'multiple': {
      if (!Array.isArray(raw)) return '多选题答案格式不正确'
      const allowed = allowedOptionValues(question.options)
      if (allowed.size > 0) {
        for (const item of raw) {
          if (!allowed.has(scalarText(item))) return '所选选项不在可选范围内'
        }
      }
      return null
    }

    case 'boolean':
      return typeof raw === 'boolean' ? null : '判断题只能选择是或否'

    case 'text':
    case 'short_text': {
      if (typeof raw !== 'string') return '该题需要填写文字'
      if (question.type === 'short_text' && (raw.includes('\n') || raw.includes('\r'))) {
        return '单行文本不能包含换行'
      }
      // 首尾空白不计入字数, 否则玩家敲几个空格就能凑够下限;
      // 按码点数而非 UTF-16 长度, 否则 emoji/生僻字在两端算出的字数不同
      const length = Array.from(raw.trim()).length
      const minLength = asInt(rules.min_length)
      if (minLength !== null && length < minLength) return `内容至少需要 ${minLength} 个字`
      const maxLength = asInt(rules.max_length)
      if (maxLength !== null && length > maxLength) return `内容最多 ${maxLength} 个字`
      return null
    }

    case 'number': {
      const number = asFloat(raw)
      if (number === null) return '请填写数字'
      const minValue = asFloat(rules.min_value)
      if (minValue !== null && number < minValue) return `数值不能小于 ${minValue}`
      const maxValue = asFloat(rules.max_value)
      if (maxValue !== null && number > maxValue) return `数值不能大于 ${maxValue}`
      return null
    }

    case 'date': {
      const picked = parseIsoDate(raw)
      if (picked === null) return '日期格式需为 YYYY-MM-DD'
      const minDate = parseIsoDate(rules.min_date)
      if (minDate !== null && picked < minDate) return `日期不能早于 ${minDate}`
      const maxDate = parseIsoDate(rules.max_date)
      if (maxDate !== null && picked > maxDate) return `日期不能晚于 ${maxDate}`
      return null
    }

    case 'rating': {
      const score = asInt(raw)
      if (score === null) return '评分必须是整数'
      // 对齐后端的 `_as_int(...) or 5`: 未配置与配成 0 都回落到满分 5
      const maxRating = asInt(rules.max_rating) || 5
      if (score < 1 || score > maxRating) return `评分需在 1 - ${maxRating} 之间`
      return null
    }

    case 'image': {
      if (!Array.isArray(raw)) return '图片答案格式不正确'
      const maxImages = asInt(rules.max_images) || 5
      if (raw.length > maxImages) return `最多上传 ${maxImages} 张图片`
      return null
    }
  }

  return null
}
