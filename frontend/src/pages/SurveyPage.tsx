import { useState, useEffect, useCallback, useMemo } from 'react'
import type { CSSProperties, FormEvent } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  ArrowLeft,
  ArrowRight,
  Send,
  AlertCircle,
  CheckCircle2,
  Copy,
  Lock,
  Clock,
  CalendarX2,
  Users,
  EyeOff,
  FileQuestion,
  Loader2,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { getSurveyByCode, submitSurvey, getSecurityConfig, unlockSurvey } from '@/lib/api'
import { isAnswered, isQuestionVisible } from '@/lib/conditions'
import { isAnswerableQuestion, validateAnswer } from '@/lib/answer-validation'
import { saveSubmission } from '@/lib/submissions-storage'
import type {
  AvailabilityState,
  PublicSurvey,
  Question,
  AnswerSubmit,
  SecurityConfig,
  SubmissionCreate,
} from '@/types/survey'
import { toast } from 'sonner'
import { QuestionCard } from '@/components/survey/QuestionCard'
import { ConfirmDialog } from '@/components/survey/ConfirmDialog'

// 非 open 状态的展示元数据。正文一律用后端下发的 availability.message
// (管理员可通过 closed_message 定制), 前端只补图标与标题, 不另造一份文案。
const AVAILABILITY_META: Record<Exclude<AvailabilityState, 'open'>, { icon: LucideIcon; title: string }> = {
  inactive: { icon: EyeOff, title: '问卷已停用' },
  unpublished: { icon: EyeOff, title: '问卷尚未发布' },
  not_started: { icon: Clock, title: '问卷尚未开始' },
  ended: { icon: CalendarX2, title: '问卷已截止' },
  full: { icon: Users, title: '名额已满' },
}

export function SurveyPage() {
  const { code } = useParams<{ code: string }>()
  const navigate = useNavigate()

  const [survey, setSurvey] = useState<PublicSurvey | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [currentIndex, setCurrentIndex] = useState(0)
  const [answers, setAnswers] = useState<Map<number, AnswerSubmit['content']>>(new Map())
  const [submitting, setSubmitting] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [submittedToken, setSubmittedToken] = useState<string | null>(null)
  const [direction, setDirection] = useState<'next' | 'prev'>('next')

  // 门禁状态
  // accessPassword 必须留在组件里: 提交端点会再校验一次 access_password, 防止绕过 unlock 直接 POST
  const [passwordInput, setPasswordInput] = useState('')
  const [accessPassword, setAccessPassword] = useState('')
  const [unlocking, setUnlocking] = useState(false)
  const [consentChecked, setConsentChecked] = useState(false)
  const [consentAccepted, setConsentAccepted] = useState(false)

  // 安全相关状态
  const [securityConfig, setSecurityConfig] = useState<SecurityConfig | null>(null)
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null)
  const [startTime] = useState<number>(() => Date.now() / 1000) // 记录开始时间（秒）

  // 可见题目由 lib/conditions 统一判定, 与后端 services/conditions.py 同源;
  // 页面内再写一份必然与后端漂移, 导致"前端没显示的题被后端判为必填未答"
  const visibleQuestions = useMemo(() => {
    if (!survey) return []
    const allQuestions = survey.questions
    // 判定输入必须是"只含可见题答案"的子集, 而不是整张 answers。后端只拿提交上来的答案
    // (即可见题答案) 建 answer_map, 前端若拿隐藏题的残留答案一起算, 嵌套分支下两端结论会相反:
    // 玩家先答 Q1=A、Q2=X 再把 Q1 改成 B, 前端还看得见 Q2=X, 后端收不到, 引用 Q2 的 Q3
    // 一边显示一边不显示, 必填时玩家直接卡死在提交那一步。
    // 用不动点迭代求这个子集: 反复"按当前可见集裁剪答案 -> 重算可见集"直到稳定, 使
    // "提交出去的答案集"与"用于判定的答案集"恒等。不删 answers 里的残留, 玩家来回切分支不丢已填内容。
    let visibleIds = new Set(allQuestions.map((question) => question.id))
    // 互相引用的病态条件可能永不收敛, 迭代次数封顶为题目数, 超出就用最后一轮结果
    for (let round = 0; round <= allQuestions.length; round += 1) {
      const scopedAnswers = new Map(
        Array.from(answers).filter(([questionId]) => visibleIds.has(questionId)),
      )
      const nextIds = new Set(
        allQuestions
          .filter((question) => isQuestionVisible(question, scopedAnswers, allQuestions))
          .map((question) => question.id),
      )
      // 两个集合都只含本卷题目 id, 逐题比对归属即可判等
      const stable = allQuestions.every(
        (question) => nextIds.has(question.id) === visibleIds.has(question.id),
      )
      visibleIds = nextIds
      if (stable) break
    }
    return allQuestions.filter((question) => visibleIds.has(question.id))
  }, [survey, answers])

  useEffect(() => {
    const fetchData = async () => {
      if (!code) return

      try {
        setLoading(true)
        // 并行获取问卷和安全配置
        const [surveyData, securityData] = await Promise.all([
          getSurveyByCode(code),
          getSecurityConfig().catch(() => null), // 安全配置获取失败不阻塞问卷加载
        ])
        setSurvey(surveyData)
        setSecurityConfig(securityData)
      } catch {
        setError('问卷不存在或已关闭')
      } finally {
        setLoading(false)
      }
    }

      fetchData()
  }, [code])

  // 题号只数收答案的题: 分节说明块占一页, 但玩家读到的不该是"第 3 题"。
  // 进度条与步数仍按全部页算, 那是"走了多少步", 与题号是两回事。
  const questionOrdinals = useMemo(() => {
    const map = new Map<number, number>()
    let ordinal = 0
    for (const q of visibleQuestions) {
      if (!isAnswerableQuestion(q)) continue
      ordinal += 1
      map.set(q.id, ordinal)
    }
    return map
  }, [visibleQuestions])

  // 可见集合会随作答收缩 (改掉前置题把后面的题藏掉), currentIndex 不钳制就会越界;
  // 越界后 currentQuestion 为 undefined, 玩家被甩到"暂无可填写的题目"页, 已填答案全丢。
  // 渲染期直接修正 state (React 官方的 adjusting state 模式), 不放 useEffect: 后者会先提交一帧错误 UI。
  const clampedIndex = visibleQuestions.length > 0 ? Math.min(currentIndex, visibleQuestions.length - 1) : 0
  if (clampedIndex !== currentIndex) {
    setCurrentIndex(clampedIndex)
  }

  const currentQuestion = visibleQuestions[clampedIndex]
  const progress = visibleQuestions.length > 0 ? ((clampedIndex + 1) / visibleQuestions.length) * 100 : 0
  const isLastQuestion = visibleQuestions.length > 0 ? clampedIndex === visibleQuestions.length - 1 : false
  const isFirstQuestion = clampedIndex === 0

  // 主题色以 CSS 变量注入页面容器: 未配置时回落到全局 --primary, 强调处只引用这一个变量
  const accentStyle = {
    '--survey-accent': survey?.theme_color || 'var(--primary)',
  } as CSSProperties

  const handleAnswerChange = useCallback((questionId: number, content: AnswerSubmit['content']) => {
    setAnswers((prev) => {
      const newAnswers = new Map(prev)
      newAnswers.set(questionId, content)
      return newAnswers
    })
  }, [])

  const handleUnlock = async (e: FormEvent) => {
    e.preventDefault()
    if (!code || unlocking) return
    // 只用 trim 判空, 发出去的仍是原文: 口令允许含空格, 两处 (unlock/submit) 必须完全一致
    if (!passwordInput.trim()) {
      toast.error('请输入访问口令')
      return
    }

    setUnlocking(true)
    try {
      const unlocked = await unlockSurvey(code, passwordInput)
      setAccessPassword(passwordInput)
      setSurvey(unlocked)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '访问口令不正确')
    } finally {
      setUnlocking(false)
    }
  }

  const handleNext = () => {
    if (!survey || visibleQuestions.length === 0) return

    // 检查必填。分节说明块不收答案, 万一被标成必填就会把玩家永久卡在这一页,
    // 后端的必填判定同样按 is_answerable 把它排除在外。
    if (currentQuestion && isAnswerableQuestion(currentQuestion) && currentQuestion.is_required && !isQuestionAnswered(currentQuestion)) {
      toast.error('请先完成当前问题')
      return
    }

    // 内容合法性就地拦下, 文案沿用后端 400 的「题目标题」+ 原因格式
    if (currentQuestion) {
      const reason = validateAnswer(currentQuestion, answers.get(currentQuestion.id))
      if (reason) {
        toast.error(`「${currentQuestion.title}」${reason}`)
        return
      }
    }

    if (isLastQuestion) {
      // 只检查可见的必填问题
      const unanswered = visibleQuestions.filter(
        (q) => isAnswerableQuestion(q) && q.is_required && !isQuestionAnswered(q)
      )
      if (unanswered.length > 0) {
        toast.error(`还有 ${unanswered.length} 道必填题未完成`)
        return
      }
      // 前面几题的答案可能在改动分支后变得不合法, 打开确认弹窗前整卷再扫一遍
      const invalid = findInvalidAnswer()
      if (invalid) {
        toast.error(`第 ${invalid.index + 1} 题「${invalid.question.title}」${invalid.reason}`)
        return
      }
      setShowConfirm(true)
    } else {
      setDirection('next')
      setCurrentIndex((prev) => prev + 1)
    }
  }

  const handlePrev = () => {
    if (!isFirstQuestion) {
      setDirection('prev')
      setCurrentIndex((prev) => prev - 1)
    }
  }

  // 有效作答判定同样走 lib/conditions, 覆盖全部 9 种题型
  const isQuestionAnswered = (question: Question): boolean => {
    return isAnswered(question, answers.get(question.id))
  }

  // 内容合法性与后端 services/question_types.py 的 validate_answer 同源。
  // 只靠后端校验的话, 玩家要答完整卷、过完 Turnstile 才吃一个 400, 这里提前找出第一道不合法的题。
  const findInvalidAnswer = (): { index: number; question: Question; reason: string } | null => {
    for (let index = 0; index < visibleQuestions.length; index += 1) {
      const question = visibleQuestions[index]
      const reason = validateAnswer(question, answers.get(question.id))
      if (reason) return { index, question, reason }
    }
    return null
  }

  const handleSubmit = async () => {
    if (!code || !survey) return

    // 检查 Turnstile 验证（如果启用）
    if (securityConfig?.turnstile_enabled && !turnstileToken) {
      toast.error('请完成安全验证')
      return
    }

    // 发请求前最后一道同源校验: 拦下就退出弹窗, 让玩家回去改, 而不是让后端回一个 400
    const invalid = findInvalidAnswer()
    if (invalid) {
      setShowConfirm(false)
      toast.error(`第 ${invalid.index + 1} 题「${invalid.question.title}」${invalid.reason}`)
      return
    }

    setSubmitting(true)
    try {
      // 只提交可见题目的答案
      const visibleQuestionIds = new Set(visibleQuestions.map(q => q.id))

      const submitData: SubmissionCreate = {
        // 玩家名不再由前端提供: 后端从绑定字段为「玩家名」的题目答案里抽取
        answers: Array.from(answers.entries())
          .filter(([questionId]) => visibleQuestionIds.has(questionId))
          .map(([questionId, content]) => ({
            question_id: questionId,
            content,
          })),
        // 安全相关字段
        turnstile_token: turnstileToken || undefined,
        start_time: startTime,
        // 门禁字段: 无口令卷不发, 后端对无口令卷一律放行
        access_password: accessPassword || undefined,
        consent: survey.require_consent ? true : undefined,
      }

      const result = await submitSurvey(code, submitData)
      // 凭据回执: 保存到本机便于查询页自动回填, 并在成功页展示提示玩家妥善保存
      setSubmittedToken(result.token)
      saveSubmission(result.token, survey.title)
      setSubmitted(true)
      setShowConfirm(false)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : '提交失败，请稍后重试'
      toast.error(errorMessage)
      // 提交失败后清除 Turnstile token
      setTurnstileToken(null)
    } finally {
      setSubmitting(false)
    }
  }

  // 加载状态
  if (loading) {
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="w-full max-w-2xl space-y-6"
        >
          <Card className="rounded-3xl">
            <CardHeader>
              <Skeleton className="h-8 w-3/4 rounded-xl" />
              <Skeleton className="h-4 w-1/2 rounded-xl mt-2" />
            </CardHeader>
            <CardContent className="space-y-4">
              <Skeleton className="h-12 rounded-2xl" />
              <Skeleton className="h-12 rounded-2xl" />
              <Skeleton className="h-12 rounded-2xl" />
            </CardContent>
          </Card>
        </motion.div>
      </div>
    )
  }

  // 错误状态
  if (error) {
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center">
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="text-center"
        >
          <motion.div
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ type: 'spring', damping: 15, delay: 0.2 }}
            className="mx-auto mb-6 w-20 h-20 rounded-full bg-destructive/10 flex items-center justify-center"
          >
            <AlertCircle className="w-10 h-10 text-destructive" />
          </motion.div>
          <h2 className="text-2xl font-bold mb-2">加载失败</h2>
          <p className="text-muted-foreground mb-6">{error}</p>
          <Button onClick={() => navigate('/')} variant="outline" className="rounded-2xl">
            <ArrowLeft className="w-4 h-4 mr-2" />
            返回列表
          </Button>
        </motion.div>
      </div>
    )
  }

  if (!survey) return null

  // 前置态优先级: 状态页 > 口令闸门 > 同意声明 > 答题流。
  // 局部常量而非直接读 survey.availability.state, 便于 TS 收窄掉 'open'
  const availability = survey.availability

  // 不可填状态: 后端此时不下发题目, 直接给结论页, 不进答题流
  if (availability.state !== 'open') {
    const meta = AVAILABILITY_META[availability.state]
    const StateIcon = meta.icon
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center" style={accentStyle}>
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="text-center max-w-md px-4"
        >
          <motion.div
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ type: 'spring', damping: 15, delay: 0.2 }}
            className="mx-auto mb-6 w-20 h-20 rounded-full bg-muted flex items-center justify-center"
          >
            <StateIcon className="w-10 h-10 text-muted-foreground" />
          </motion.div>
          <h2 className="text-2xl font-bold mb-2" style={{ color: 'var(--survey-accent)' }}>
            {survey.title}
          </h2>
          <p className="text-lg font-medium mb-2">{meta.title}</p>
          {availability.message && (
            <p className="text-muted-foreground mb-6 whitespace-pre-wrap">{availability.message}</p>
          )}
          <Button onClick={() => navigate('/')} variant="outline" className="rounded-2xl">
            <ArrowLeft className="w-4 h-4 mr-2" />
            返回列表
          </Button>
        </motion.div>
      </div>
    )
  }

  // 口令闸门: locked 时后端不下发 questions, 必须先换回完整问卷才能答题
  if (survey.locked) {
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center" style={accentStyle}>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="w-full max-w-md px-4"
        >
          <Card className="rounded-3xl border-border/50 shadow-lg">
            <CardHeader className="text-center">
              <motion.div
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                transition={{ type: 'spring', damping: 15, delay: 0.1 }}
                className="mx-auto mb-2 w-14 h-14 rounded-2xl flex items-center justify-center"
                style={{ backgroundColor: 'color-mix(in oklab, var(--survey-accent) 12%, transparent)' }}
              >
                <Lock className="w-7 h-7" style={{ color: 'var(--survey-accent)' }} />
              </motion.div>
              <CardTitle className="text-xl">{survey.title}</CardTitle>
              <CardDescription>该问卷需要访问口令，请向发放者索取后填写</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleUnlock} className="space-y-4">
                <Input
                  type="password"
                  autoFocus
                  autoComplete="off"
                  placeholder="请输入访问口令"
                  value={passwordInput}
                  onChange={(e) => setPasswordInput(e.target.value)}
                  className="h-12 rounded-2xl text-base"
                />
                <Button type="submit" disabled={unlocking} className="w-full rounded-2xl h-12">
                  {unlocking ? (
                    <>
                      <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                      校验中...
                    </>
                  ) : (
                    <>
                      进入问卷
                      <ArrowRight className="w-4 h-4 ml-2" />
                    </>
                  )}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => navigate('/')}
                  className="w-full rounded-2xl"
                >
                  <ArrowLeft className="w-4 h-4 mr-2" />
                  返回列表
                </Button>
              </form>
            </CardContent>
          </Card>
        </motion.div>
      </div>
    )
  }

  // 同意声明: 勾选前不进答题流, 提交时随 consent: true 一起发
  if (survey.require_consent && !consentAccepted) {
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center py-4" style={accentStyle}>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="w-full max-w-2xl px-4"
        >
          <Card className="rounded-3xl border-border/50 shadow-lg">
            <CardHeader>
              <CardTitle className="text-xl" style={{ color: 'var(--survey-accent)' }}>
                {survey.title}
              </CardTitle>
              <CardDescription>请先阅读以下声明，同意后开始填写</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="max-h-72 overflow-y-auto rounded-2xl border border-border/50 bg-muted/30 p-4 text-sm leading-relaxed whitespace-pre-wrap">
                {survey.privacy_notice || '管理员未填写声明正文，勾选即表示你同意提交所填写的内容。'}
              </div>

              <Label
                htmlFor="survey-consent"
                className="flex items-center space-x-3 p-4 rounded-2xl border border-border/50 cursor-pointer transition-all duration-200 hover:bg-accent/50 hover:border-primary/30 has-[input:checked]:bg-primary/5 has-[input:checked]:border-primary/50"
              >
                <Checkbox
                  id="survey-consent"
                  checked={consentChecked}
                  onCheckedChange={(checked) => setConsentChecked(checked === true)}
                />
                <span className="flex-1 text-base">我已阅读并同意上述声明</span>
              </Label>

              <div className="flex items-center justify-between gap-3">
                <Button variant="outline" onClick={() => navigate('/')} className="rounded-2xl h-12 px-6">
                  <ArrowLeft className="w-4 h-4 mr-2" />
                  返回列表
                </Button>
                <Button
                  onClick={() => setConsentAccepted(true)}
                  disabled={!consentChecked}
                  className="rounded-2xl h-12 px-6"
                >
                  开始填写
                  <ArrowRight className="w-4 h-4 ml-2" />
                </Button>
              </div>
            </CardContent>
          </Card>
        </motion.div>
      </div>
    )
  }

  // 提交成功状态。必须挡在答题流之前: 提交完成后 availability 仍是 open, 不拦就会退回题目页
  if (submitted) {
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center" style={accentStyle}>
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ type: 'spring', damping: 20 }}
          className="text-center max-w-md"
        >
          <motion.div
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{
              type: 'spring',
              damping: 12,
              stiffness: 200,
              delay: 0.2
            }}
            className="mx-auto mb-6 w-24 h-24 rounded-full bg-green-500/10 flex items-center justify-center"
          >
            <motion.div
              initial={{ scale: 0, rotate: -180 }}
              animate={{ scale: 1, rotate: 0 }}
              transition={{ delay: 0.4, type: 'spring', damping: 15 }}
            >
              <CheckCircle2 className="w-12 h-12 text-green-500" />
            </motion.div>
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.5 }}
          >
            <h2 className="text-3xl font-bold mb-3">提交成功！</h2>
            <p className="text-muted-foreground mb-6 text-lg whitespace-pre-wrap">
              {survey.success_message
                || (survey.requires_review === false ? '感谢您的填写，我们已收到～' : '感谢您的填写，请等待管理员审核')}
            </p>

            {submittedToken && (survey.requires_review || survey.issues_code) && (
              <div className="mb-8 text-left rounded-2xl border border-amber-500/40 bg-amber-500/5 p-4">
                <p className="text-sm font-medium mb-2">请妥善保存您的查询凭据</p>
                <p className="text-xs text-muted-foreground mb-3">
                  凭此凭据可查询审核进度{survey.issues_code ? '，并在通过后领取进服注册码' : ''}。本浏览器已自动记住，
                  但清除浏览器数据或更换设备后将丢失，建议另行保存。
                </p>
                <div className="flex gap-2">
                  <Input
                    readOnly
                    value={submittedToken}
                    className="font-mono text-xs rounded-xl"
                    onFocus={(e) => e.currentTarget.select()}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    aria-label="复制凭据"
                    className="rounded-xl shrink-0"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(submittedToken)
                        toast.success('凭据已复制')
                      } catch {
                        toast.error('复制失败，请手动选择复制')
                      }
                    }}
                  >
                    <Copy className="w-4 h-4" />
                  </Button>
                </div>
              </div>
            )}

            <Button
              onClick={() => navigate('/')}
              size="lg"
              className="rounded-2xl h-12 px-8"
            >
              返回列表
            </Button>
          </motion.div>
        </motion.div>
      </div>
    )
  }

  // 开放且已解锁, 却一道可见题都没有 (卷是空的 / 条件把题全隐藏了): 给结论页而不是白屏
  if (!currentQuestion) {
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center" style={accentStyle}>
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="text-center max-w-md px-4"
        >
          <div className="mx-auto mb-6 w-20 h-20 rounded-full bg-muted flex items-center justify-center">
            <FileQuestion className="w-10 h-10 text-muted-foreground" />
          </div>
          <h2 className="text-2xl font-bold mb-2">{survey.title}</h2>
          <p className="text-muted-foreground mb-6">该问卷暂无可填写的题目</p>
          <Button onClick={() => navigate('/')} variant="outline" className="rounded-2xl">
            <ArrowLeft className="w-4 h-4 mr-2" />
            返回列表
          </Button>
        </motion.div>
      </div>
    )
  }

  return (
    <div className="min-h-[calc(100vh-10rem)] py-4" style={accentStyle}>
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-2xl mx-auto"
      >
        {/* 问卷标题和进度 */}
        <motion.div
          initial={{ opacity: 0, y: -20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
          className="mb-6"
        >
          <div className="flex items-center justify-between mb-4">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => navigate('/')}
              className="rounded-xl"
            >
              <ArrowLeft className="w-4 h-4 mr-2" />
              退出
            </Button>
            <Badge variant="secondary" className="rounded-full px-3">
              {clampedIndex + 1} / {visibleQuestions.length}
            </Badge>
          </div>

          <h1 className="text-2xl font-bold mb-2" style={{ color: 'var(--survey-accent)' }}>{survey.title}</h1>
          {survey.description && (
            <p className="text-muted-foreground">{survey.description}</p>
          )}

          {/* 只在进度条这一块把 --primary 顶成主题色: 覆盖整页会连按钮前景色一起换掉, 易出对比度事故 */}
          <div className="mt-4" style={{ '--primary': 'var(--survey-accent)' } as CSSProperties}>
            <Progress value={progress} className="h-2 rounded-full" />
          </div>
        </motion.div>

        {/* 问题卡片 */}
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={currentQuestion.id}
            initial={{
              opacity: 0,
              x: direction === 'next' ? 100 : -100,
              scale: 0.95
            }}
            animate={{
              opacity: 1,
              x: 0,
              scale: 1
            }}
            exit={{
              opacity: 0,
              x: direction === 'next' ? -100 : 100,
              scale: 0.95
            }}
            transition={{
              duration: 0.3,
              ease: [0.25, 0.46, 0.45, 0.94]
            }}
          >
            <QuestionCard
              question={currentQuestion}
              value={answers.get(currentQuestion.id)}
              onChange={(content: AnswerSubmit['content']) => handleAnswerChange(currentQuestion.id, content)}
              // QuestionCard 内渲染的是 index + 1, 故传"题号 - 1"; 分节块用不到这个值
              index={(questionOrdinals.get(currentQuestion.id) ?? 1) - 1}
            />
          </motion.div>
        </AnimatePresence>

        {/* 导航按钮 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3 }}
          className="flex items-center justify-between mt-6"
        >
          <Button
            variant="outline"
            onClick={handlePrev}
            disabled={isFirstQuestion}
            className="rounded-2xl h-12 px-6"
          >
            <ArrowLeft className="w-4 h-4 mr-2" />
            上一题
          </Button>

          <Button
            onClick={handleNext}
            className="rounded-2xl h-12 px-6"
          >
            {isLastQuestion ? (
              <>
                提交问卷
                <Send className="w-4 h-4 ml-2" />
              </>
            ) : (
              <>
                下一题
                <ArrowRight className="w-4 h-4 ml-2" />
              </>
            )}
          </Button>
        </motion.div>
      </motion.div>

      {/* 确认提交弹窗 */}
      <ConfirmDialog
        open={showConfirm}
        onOpenChange={setShowConfirm}
        onSubmit={handleSubmit}
        submitting={submitting}
        turnstileEnabled={securityConfig?.turnstile_enabled}
        turnstileSiteKey={import.meta.env.VITE_TURNSTILE_SITE_KEY}
        turnstileVerified={!!turnstileToken}
        onTurnstileVerify={setTurnstileToken}
      />
    </div>
  )
}
