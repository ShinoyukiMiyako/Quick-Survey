import type {
  AnswerSubmit,
  ConditionOperator,
  ConditionRule,
  Question,
  QuestionCondition,
  QuestionType,
} from '@/types/survey'

// 条件引擎: 与后端 app/services/conditions.py 逐条对齐。
// 两端各算一次可见性 (前端决定题目展示与必填, 后端决定入库校验), 语义必须完全一致,
// 否则会出现"前端没显示的题被后端判为必填未答"这类无法自证的提交失败。

type AnswerContent = AnswerSubmit['content']

export interface NormalizedCondition {
  action: 'show' | 'hide'
  match: 'all' | 'any'
  rules: ConditionRule[]
}

const OPERATORS: readonly ConditionOperator[] = [
  'eq',
  'neq',
  'in',
  'not_in',
  'contains',
  'gt',
  'lt',
  'answered',
  'not_answered',
]

function isOperator(v: unknown): v is ConditionOperator {
  return typeof v === 'string' && (OPERATORS as readonly string[]).includes(v)
}

/**
 * 把新旧两种 condition 形态归一成 {action, match, rules}。
 * 旧形态 {depends_on, show_when} 归一为 match=any 的单条 in 规则 —— 单值/多值语义都能覆盖。
 * 非法或规则为空一律返回 null (视为无条件, 恒可见)。
 */
export function normalizeCondition(c?: QuestionCondition | null): NormalizedCondition | null {
  if (!c || typeof c !== 'object') return null

  // 新形态优先: 编辑器保存时两套字段可能同时残留, 以 rules 为准
  if (Array.isArray(c.rules)) {
    const rules = c.rules.filter(
      (r): r is ConditionRule => !!r && typeof r.question_id === 'number' && isOperator(r.operator),
    )
    if (rules.length === 0) return null
    return {
      action: c.action === 'hide' ? 'hide' : 'show',
      match: c.match === 'any' ? 'any' : 'all',
      rules,
    }
  }

  // 旧形态 (存量库里全是这个)
  if (typeof c.depends_on === 'number' && c.show_when !== undefined && c.show_when !== null) {
    const values = (Array.isArray(c.show_when) ? c.show_when : [c.show_when]).map(String)
    if (values.length === 0) return null
    return {
      action: 'show',
      match: 'any',
      rules: [{ question_id: c.depends_on, operator: 'in', value: values }],
    }
  }

  return null
}

/**
 * 有效作答判定, 覆盖全部题型。空串/空数组/未填都算未答;
 * number 的 0 与 boolean 的 false 是合法答案, 必须算已答。
 */
export function isAnswered(question: Question, content?: AnswerContent | null): boolean {
  return isAnsweredByType(question.type, content)
}

function isAnsweredByType(qtype: QuestionType, content?: AnswerContent | null): boolean {
  if (!content) return false

  switch (qtype) {
    case 'single':
    case 'select':
    case 'date':
      return typeof content.value === 'string' && content.value.trim() !== ''
    case 'boolean':
      return typeof content.value === 'boolean'
    case 'number':
    case 'rating':
      // 存量数据里数字可能是字符串, 一并接受; 0 必须算已答
      if (typeof content.value === 'number') return Number.isFinite(content.value)
      return typeof content.value === 'string' && content.value.trim() !== ''
    case 'multiple':
      return Array.isArray(content.values) && content.values.length > 0
    case 'text':
    case 'short_text':
      return typeof content.text === 'string' && content.text.trim() !== ''
    case 'image':
      return Array.isArray(content.images) && content.images.length > 0
    case 'file':
      return Array.isArray(content.files) && content.files.length > 0
    default:
      return false
  }
}

/**
 * 标量归一化, 对齐后端 question_types._scalar_text 与 conditions._scalar_text:
 * 首尾空白一律去掉, 去掉后为空的等同未作答 (返回 null)。
 * 后端在取可比较值与摊平比较项两层都做了 strip, 前端漏掉的话玩家在依赖题里多敲一个空格,
 * 前端判隐藏而后端判可见, 那道必填题就成了看不见又必须答的死结, 提交必吃 400 且无从自证。
 */
function scalarText(v: unknown): string | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  const t = String(v).trim()
  return t === '' ? null : t
}

/** 列表归一化: 逐项 strip 并丢掉空项, 全空视为未作答 —— 后端 comparable_value 的列表分支同款 */
function listText(raw?: unknown[] | null): string[] | null {
  const items = (raw ?? []).map(scalarText).filter((t): t is string => t !== null)
  return items.length > 0 ? items : null
}

/**
 * 取出用于条件比较的值。boolean 归一化为小写 'true'/'false' ——
 * 这是修复"后端 str(True)=='True' 与编辑器写入的 show_when 'true' 永不相等"的关键点。
 * multiple/image/file 返回字符串数组, 其余返回字符串, 未作答返回 null。
 */
function comparableValue(qtype: QuestionType, content?: AnswerContent | null): string | string[] | null {
  if (!isAnsweredByType(qtype, content) || !content) return null

  switch (qtype) {
    case 'multiple':
      return listText(content.values)
    case 'image':
      // image 在后端 condition_source 为 false, 不该被引用为依赖题;
      // 这里仍返回非 null, 保证误配时 answered/not_answered 的判定不反过来
      return listText(content.images)
    case 'file':
      // 同 image: 不该作依赖题, 但误配时也要能答出"答没答"; 取原始文件名而不是存储地址
      return listText((content.files ?? []).map((f) => f?.name || f?.url))
    case 'boolean':
      return content.value === true ? 'true' : 'false'
    case 'text':
    case 'short_text':
      return scalarText(content.text)
    default:
      return scalarText(content.value)
  }
}

// 与 Python float() 对齐: 空串/非数字返回 null, 而不是 JS Number('') === 0
function toFloat(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string') {
    const t = v.trim()
    if (t === '') return null
    const n = Number(t)
    return Number.isFinite(n) ? n : null
  }
  return null
}

function toStringSet(v: ConditionRule['value']): Set<string> {
  if (v === undefined || v === null) return new Set()
  // 规则里配的值同样要 strip: 后端 _to_items 对 expected 也走了一遍 _scalar_text,
  // 编辑器里存成 "A " 时前端不去空白就会与答案 "A" 比不上, 与后端结论相反
  const raw: unknown[] = Array.isArray(v) ? v : [v]
  return new Set(raw.map(scalarText).filter((t): t is string => t !== null))
}

// 多选按"交集非空", 单值按集合成员
function inTargets(actual: string | string[], targets: Set<string>): boolean {
  if (Array.isArray(actual)) return actual.some((v) => targets.has(v))
  return targets.has(actual)
}

function containsTarget(actual: string | string[], target: string): boolean {
  const needle = target.toLowerCase()
  if (Array.isArray(actual)) return actual.some((v) => v.toLowerCase().includes(needle))
  return actual.toLowerCase().includes(needle)
}

/**
 * 求值单条规则。返回 null 表示"不判定"(依赖题不在本卷: 已删 / 随机卷未抽中),
 * 由调用方按 match 决定是跳过还是记为不成立。
 */
function evaluateRule(
  rule: ConditionRule,
  answers: Map<number, AnswerContent>,
  questionMap: Map<number, Question>,
): boolean | null {
  const depend = questionMap.get(rule.question_id)
  if (!depend) return null

  const actual = comparableValue(depend.type, answers.get(rule.question_id))

  if (actual === null) {
    if (rule.operator === 'not_answered') return true
    return false
  }

  switch (rule.operator) {
    case 'answered':
      return true
    case 'not_answered':
      return false
    case 'eq':
    case 'neq':
    case 'in':
    case 'not_in': {
      // 四个运算符统一按集合命中, 与后端 _evaluate_rule 逐字对齐: 依赖题是多选时可比较值
      // 是数组, eq 也必须退化成"命中任一"; 反过来 eq 配了数组值也按"命中任一"而不是
      // 拿 String(['A','B']) 去比字面量, 否则同一份条件两端会算出相反的可见性。
      const hit = inTargets(actual, toStringSet(rule.value))
      return rule.operator === 'eq' || rule.operator === 'in' ? hit : !hit
    }
    case 'contains': {
      // 关键词没配就当规则未配置, 不做无意义的全命中 —— JS 的 ''.includes() 恒为真,
      // 不拦住的话一条空 contains 会让整条分支永远成立, 而后端那边是永远不成立。
      if (Array.isArray(rule.value)) return false
      const needle = rule.value === undefined || rule.value === null ? '' : String(rule.value).trim()
      if (needle === '') return false
      return containsTarget(actual, needle)
    }
    case 'gt':
    case 'lt': {
      // 双方都能转 float 才比较, 否则一律不成立。依赖题是多选时后端逐项比较、命中任一即成立,
      // 前端整体判 false 会把 {"values":["3","7"]} 对 gt 5 这类分支藏掉, 而后端认为该题可见且必填
      const right = toFloat(rule.value)
      if (right === null) return false
      const isGt = rule.operator === 'gt'
      const items = Array.isArray(actual) ? actual : [actual]
      return items.some((item) => {
        const left = toFloat(item)
        if (left === null) return false
        return isGt ? left > right : left < right
      })
    }
    default:
      return false
  }
}

/**
 * 判断题目在当前作答状态下是否可见。
 * 全部规则都因依赖题缺失而无法判定时返回 true —— 不因悬空条件把题藏掉, 与后端一致。
 */
export function isQuestionVisible(
  question: Question,
  answers: Map<number, AnswerContent>,
  allQuestions: Question[],
): boolean {
  const condition = normalizeCondition(question.condition)
  if (!condition) return true

  const questionMap = new Map(allQuestions.map((q) => [q.id, q]))

  let considered = 0
  let matched = condition.match === 'all'

  for (const rule of condition.rules) {
    const result = evaluateRule(rule, answers, questionMap)
    // 不判定: all 下跳过该规则, any 下不贡献成立 (等价于记为不成立)
    if (result === null) continue
    considered += 1
    if (condition.match === 'all') {
      if (!result) matched = false
    } else if (result) {
      matched = true
    }
  }

  if (considered === 0) return true

  return condition.action === 'hide' ? !matched : matched
}
