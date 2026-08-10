import { useState, useCallback, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Search, Loader2, AlertCircle, KeyRound, Copy, Trash2 } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { StatusTimeline } from './StatusTimeline'
import { querySubmissionStatus, redeemRegistrationCode } from '@/lib/api'
import { REGISTRATION_CODE_ENABLED } from '@/lib/feature-flags'
import {
  getStoredSubmissions,
  saveSubmission,
  clearStoredSubmissions,
} from '@/lib/submissions-storage'
import type { SubmissionStatus, RegistrationCodeResult } from '@/types/survey'
import { toast } from 'sonner'

interface QueryDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

// 按 token 去重合并: 同 token 用新结果替换, 最新查询的置顶
function mergeResult(list: SubmissionStatus[], next: SubmissionStatus): SubmissionStatus[] {
  return [next, ...list.filter((s) => s.token !== next.token)]
}

export function QueryDialog({ open, onOpenChange }: QueryDialogProps) {
  const [token, setToken] = useState('')
  const [loading, setLoading] = useState(false)
  const [autoLoading, setAutoLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<SubmissionStatus[]>([])
  // 每个 token 的领码结果 (明文码或已领取标记), 仅展示, 不持久化
  const [codes, setCodes] = useState<Record<string, RegistrationCodeResult>>({})
  const [redeeming, setRedeeming] = useState<Record<string, boolean>>({})
  // 二次确认: 注册码一次性且仅 24H 有效, 领取前要求显式确认并设 3 秒冷却, 防手滑误领后过期作废
  const [confirmToken, setConfirmToken] = useState<string | null>(null)
  const [cooldown, setCooldown] = useState(0)

  // 冷却倒计时: 进入二次确认后逐秒递减至 0, 期间确认按钮禁用
  useEffect(() => {
    if (confirmToken === null || cooldown <= 0) return
    const timer = setTimeout(() => setCooldown((c) => c - 1), 1000)
    return () => clearTimeout(timer)
  }, [confirmToken, cooldown])

  // 打开时自动回填本机记住的提交并查询其最新状态
  useEffect(() => {
    if (!open) return
    const stored = getStoredSubmissions()
    if (stored.length === 0) return

    setAutoLoading(true)
    Promise.allSettled(stored.map((s) => querySubmissionStatus(s.token)))
      .then((settled) => {
        const ok = settled
          .filter((r): r is PromiseFulfilledResult<SubmissionStatus> => r.status === 'fulfilled')
          .map((r) => r.value)
        setResults(ok)
      })
      .finally(() => setAutoLoading(false))
  }, [open])

  const handleSearch = useCallback(async () => {
    const trimmed = token.trim()
    if (!trimmed) {
      setError('请输入查询凭据')
      return
    }

    setLoading(true)
    setError(null)
    try {
      const sub = await querySubmissionStatus(trimmed)
      setResults((prev) => mergeResult(prev, sub))
      // 手动查到的也记住, 方便下次自动回填
      saveSubmission(sub.token, sub.survey_title)
      setToken('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '查询失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [token])

  // 点击"领取注册码": 不直接领, 先进入二次确认并启动 3 秒冷却
  const startConfirm = useCallback((token: string) => {
    setConfirmToken(token)
    setCooldown(3)
  }, [])

  const cancelConfirm = useCallback(() => {
    setConfirmToken(null)
    setCooldown(0)
  }, [])

  const handleRedeem = useCallback(async (sub: SubmissionStatus) => {
    setRedeeming((prev) => ({ ...prev, [sub.token]: true }))
    try {
      const result = await redeemRegistrationCode(sub.token)
      setCodes((prev) => ({ ...prev, [sub.token]: result }))
      // 领取后更新该条状态, 隐藏按钮; 退出二次确认态
      setResults((prev) =>
        prev.map((s) =>
          s.token === sub.token ? { ...s, code_issued: true, can_get_code: false } : s,
        ),
      )
      setConfirmToken(null)
      setCooldown(0)
      if (result.registration_code) {
        toast.success('注册码已生成')
      }
    } catch (err) {
      // 领取失败保留二次确认面板, 便于重试 (冷却已结束, 可直接再次确认)
      toast.error(err instanceof Error ? err.message : '领取失败，请稍后重试')
    } finally {
      setRedeeming((prev) => ({ ...prev, [sub.token]: false }))
    }
  }, [])

  const copyText = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success('已复制')
    } catch {
      toast.error('复制失败，请手动选择复制')
    }
  }, [])

  const handleClearLocal = useCallback(() => {
    clearStoredSubmissions()
    setResults([])
    setCodes({})
    toast.success('已清除本机记录')
  }, [])

  const handleClose = () => {
    onOpenChange(false)
    setTimeout(() => {
      setToken('')
      setError(null)
      setResults([])
      setCodes({})
      setRedeeming({})
      setConfirmToken(null)
      setCooldown(0)
    }, 200)
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent
        className="sm:max-w-lg md:max-w-xl bg-background/95 backdrop-blur-xl border-border/50 rounded-2xl"
        showCloseButton={true}
      >
        <DialogHeader>
          <DialogTitle className="text-xl font-semibold flex items-center gap-2">
            <Search className="w-5 h-5 text-primary" />
            查询进度
          </DialogTitle>
          <DialogDescription>
            输入提交成功后获得的查询凭据，查看审核状态{REGISTRATION_CODE_ENABLED ? '并领取注册码' : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 pt-2">
          {/* 凭据输入 */}
          <div className="flex gap-2">
            <div className="relative flex-1">
              <KeyRound className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
              <Input
                placeholder="粘贴查询凭据"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !loading) handleSearch()
                }}
                className="pl-9 h-11 rounded-xl font-mono text-xs"
                disabled={loading}
              />
            </div>
            <Button
              onClick={handleSearch}
              disabled={loading || !token.trim()}
              className="h-11 px-6 rounded-xl"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
            </Button>
          </div>

          {/* 结果区域 */}
          <AnimatePresence mode="wait">
            {(loading || autoLoading) && (
              <motion.div
                key="loading"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="flex items-center justify-center py-8"
              >
                <Loader2 className="w-6 h-6 animate-spin text-primary" />
              </motion.div>
            )}

            {error && !loading && (
              <motion.div
                key="error"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="flex items-center gap-2 p-4 rounded-xl bg-destructive/10 text-destructive"
              >
                <AlertCircle className="w-5 h-5 shrink-0" />
                <span className="text-sm">{error}</span>
              </motion.div>
            )}

            {results.length > 0 && !loading && !autoLoading && (
              <motion.div
                key="results"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="space-y-4"
              >
                {results.map((submission, index) => (
                  <motion.div
                    key={submission.token}
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: index * 0.05 }}
                    className="p-4 rounded-xl bg-muted/30 border border-border/50"
                  >
                    {submission.survey_title && (
                      <div className="mb-4 pb-3 border-b border-border/50">
                        <h3 className="font-medium text-sm text-muted-foreground">
                          {submission.survey_title}
                        </h3>
                      </div>
                    )}

                    <StatusTimeline submission={submission} />

                    {/* 领码区: 可领 -> 按钮; 刚领到 -> 展示码; 已领过 -> 提示。
                        注册码停用期间 (REGISTRATION_CODE_ENABLED=false) 整块短路, 过审只给进服指引 */}
                    {!REGISTRATION_CODE_ENABLED ? (
                      submission.status === 'approved' ? (
                        <div className="mt-4 rounded-xl border border-primary/40 bg-primary/5 p-3">
                          <p className="text-xs text-muted-foreground">
                            审核已通过，<span className="font-medium text-foreground">直接进服即可，不需要注册码</span>。
                            首次进入服务器后，在游戏里输入{' '}
                            <span className="font-mono">/register 你的密码 你的密码</span>{' '}
                            设置密码（同一个密码打两遍），之后每次进服用{' '}
                            <span className="font-mono">/login 你的密码</span> 登录。
                          </p>
                        </div>
                      ) : null
                    ) : codes[submission.token]?.registration_code ? (
                      <div className="mt-4 rounded-xl border border-primary/40 bg-primary/5 p-3">
                        <p className="text-xs text-muted-foreground mb-2">
                          请在游戏内使用 <span className="font-mono">/register &lt;密码&gt; &lt;确认&gt; &lt;注册码&gt;</span> 完成注册。
                          一次性、仅限该用户名
                          {codes[submission.token].code_expires_minutes
                            ? `，${Math.round((codes[submission.token].code_expires_minutes as number) / 60)} 小时内有效`
                            : ''}
                          。关闭后无法再次查看，请尽快使用。
                        </p>
                        <div className="flex items-center gap-2">
                          <Input
                            readOnly
                            value={codes[submission.token].registration_code}
                            className="font-mono text-base tracking-widest rounded-xl"
                            onFocus={(e) => e.currentTarget.select()}
                          />
                          <Button
                            type="button"
                            variant="outline"
                            size="icon"
                            aria-label="复制注册码"
                            className="rounded-xl shrink-0"
                            onClick={() => copyText(codes[submission.token].registration_code as string)}
                          >
                            <Copy className="w-4 h-4" />
                          </Button>
                        </div>
                      </div>
                    ) : submission.can_get_code ? (
                      confirmToken === submission.token ? (
                        <div className="mt-4 rounded-xl border border-amber-500/40 bg-amber-500/5 p-3 space-y-3">
                          <div className="flex gap-2 text-xs text-muted-foreground">
                            <AlertCircle className="w-4 h-4 shrink-0 text-amber-500" />
                            <p>
                              注册码<span className="font-medium text-foreground">仅 24 小时内有效</span>，且为一次性使用。
                              请确保你能在有效期内进服完成 <span className="font-mono">/register</span> 注册；
                              若暂时无法及时进服，请<span className="font-medium text-foreground">不要现在领取</span>，
                              以免过期失效需联系管理员补发。
                            </p>
                          </div>
                          <div className="flex gap-2">
                            <Button
                              variant="outline"
                              onClick={cancelConfirm}
                              disabled={redeeming[submission.token]}
                              className="flex-1 rounded-xl"
                            >
                              取消
                            </Button>
                            <Button
                              onClick={() => handleRedeem(submission)}
                              disabled={cooldown > 0 || redeeming[submission.token]}
                              className="flex-1 rounded-xl"
                            >
                              {redeeming[submission.token] ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                              ) : cooldown > 0 ? (
                                `确认领取 (${cooldown})`
                              ) : (
                                '确认领取'
                              )}
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <Button
                          onClick={() => startConfirm(submission.token)}
                          disabled={redeeming[submission.token]}
                          className="mt-4 w-full rounded-xl"
                        >
                          <KeyRound className="w-4 h-4" />
                          领取注册码
                        </Button>
                      )
                    ) : submission.code_issued ? (
                      <p className="mt-4 text-xs text-muted-foreground">
                        注册码已领取过。如遗失或已过期，请联系管理员补发。
                      </p>
                    ) : null}
                  </motion.div>
                ))}

                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleClearLocal}
                  className="w-full rounded-xl text-muted-foreground"
                >
                  <Trash2 className="w-4 h-4" />
                  这不是我 / 清除本机记录
                </Button>
              </motion.div>
            )}

            {!loading && !autoLoading && !error && results.length === 0 && (
              <motion.div
                key="initial"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="text-center py-6 text-muted-foreground text-sm"
              >
                粘贴查询凭据后点击搜索查询
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </DialogContent>
    </Dialog>
  )
}
