import { useState, useEffect, useCallback, useMemo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { ArrowLeft, ArrowRight, Send, AlertCircle, CheckCircle2, Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { getSurveyByCode, submitSurvey, getSecurityConfig } from '@/lib/api'
import { saveSubmission } from '@/lib/submissions-storage'
import type { PublicSurvey, Question, AnswerSubmit, SecurityConfig } from '@/types/survey'
import { toast } from 'sonner'
import { QuestionCard } from '@/components/survey/QuestionCard'
import { ConfirmDialog } from '@/components/survey/ConfirmDialog'

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
  
  // 安全相关状态
  const [securityConfig, setSecurityConfig] = useState<SecurityConfig | null>(null)
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null)
  const [startTime] = useState<number>(() => Date.now() / 1000) // 记录开始时间（秒）

  // 检查条件题目是否应该显示
  // depends_on 语义: 依赖题的 question_id（稳定引用，不随题序/编辑变化）— 与后端 public.py 一致
  // 答案字段兼容: single/boolean → value, multiple → values, text → text
  const isQuestionVisible = useCallback((question: Question, currentAnswers: Map<number, AnswerSubmit['content']>, allQuestions: Question[]): boolean => {
    const condition = question.condition
    if (!condition) return true

    // 依赖题不存在（已删/随机卷未抽中）时不因条件隐藏, 与后端一致
    const dependQuestion = allQuestions.find(q => q.id === condition.depends_on)
    if (!dependQuestion) return true

    const dependAnswer = currentAnswers.get(dependQuestion.id)
    if (!dependAnswer) return false

    // 根据答案字段兼容不同题型
    let answerValue: string | string[] | undefined
    if (dependAnswer.value !== undefined && dependAnswer.value !== '') {
      answerValue = String(dependAnswer.value)
    } else if (Array.isArray(dependAnswer.values) && dependAnswer.values.length > 0) {
      answerValue = dependAnswer.values.map(String)
    } else if (typeof dependAnswer.text === 'string' && dependAnswer.text.trim() !== '') {
      answerValue = dependAnswer.text
    } else {
      return false
    }

    const showWhen = condition.show_when
    const showSet = new Set(Array.isArray(showWhen) ? showWhen.map(String) : [String(showWhen)])

    if (Array.isArray(answerValue)) {
      return answerValue.some(v => showSet.has(v))
    }
    return showSet.has(answerValue)
  }, [])

  // 计算可见题目列表
  const visibleQuestions = useMemo(() => {
    if (!survey) return []
    return survey.questions.filter(question => isQuestionVisible(question, answers, survey.questions))
  }, [survey, answers, isQuestionVisible])

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

  const currentQuestion = visibleQuestions[currentIndex]
  const progress = visibleQuestions.length > 0 ? ((currentIndex + 1) / visibleQuestions.length) * 100 : 0
  const isLastQuestion = visibleQuestions.length > 0 ? currentIndex === visibleQuestions.length - 1 : false
  const isFirstQuestion = currentIndex === 0

  const handleAnswerChange = useCallback((questionId: number, content: AnswerSubmit['content']) => {
    setAnswers((prev) => {
      const newAnswers = new Map(prev)
      newAnswers.set(questionId, content)
      return newAnswers
    })
  }, [])

  const handleNext = () => {
    if (!survey || visibleQuestions.length === 0) return

    // 检查必填
    if (currentQuestion?.is_required && !isQuestionAnswered(currentQuestion)) {
      toast.error('请先完成当前问题')
      return
    }

    if (isLastQuestion) {
      // 只检查可见的必填问题
      const unanswered = visibleQuestions.filter(
        (q) => q.is_required && !isQuestionAnswered(q)
      )
      if (unanswered.length > 0) {
        toast.error(`还有 ${unanswered.length} 道必填题未完成`)
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

  const isQuestionAnswered = (question: Question): boolean => {
    const answer = answers.get(question.id)
    if (!answer) return false

    switch (question.type) {
      case 'single':
      case 'boolean':
        return answer.value !== undefined && answer.value !== ''
      case 'multiple':
        return Array.isArray(answer.values) && answer.values.length > 0
      case 'text':
        return typeof answer.text === 'string' && answer.text.trim() !== ''
      case 'image':
        return Array.isArray(answer.images) && answer.images.length > 0
      default:
        return false
    }
  }

  const handleSubmit = async () => {
    if (!code || !survey) return

    // 检查 Turnstile 验证（如果启用）
    if (securityConfig?.turnstile_enabled && !turnstileToken) {
      toast.error('请完成安全验证')
      return
    }

    setSubmitting(true)
    try {
      // 只提交可见题目的答案
      const visibleQuestionIds = new Set(visibleQuestions.map(q => q.id))
      
      const submitData = {
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

  // 提交成功状态
  if (submitted) {
    return (
      <div className="min-h-[calc(100vh-10rem)] flex items-center justify-center">
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
            <p className="text-muted-foreground mb-6 text-lg">
              感谢您的填写，请等待管理员审核
            </p>

            {submittedToken && (
              <div className="mb-8 text-left rounded-2xl border border-amber-500/40 bg-amber-500/5 p-4">
                <p className="text-sm font-medium mb-2">请妥善保存您的查询凭据</p>
                <p className="text-xs text-muted-foreground mb-3">
                  凭此凭据可查询审核进度，并在通过后领取进服注册码。本浏览器已自动记住，
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

  if (!survey || !currentQuestion) return null

  return (
    <div className="min-h-[calc(100vh-10rem)] py-4">
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
              {currentIndex + 1} / {visibleQuestions.length}
            </Badge>
          </div>

          <h1 className="text-2xl font-bold mb-2">{survey.title}</h1>
          {survey.description && (
            <p className="text-muted-foreground">{survey.description}</p>
          )}

          <div className="mt-4">
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
              index={currentIndex}
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
