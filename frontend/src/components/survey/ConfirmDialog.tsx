import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Send, Loader2, User, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Turnstile } from '@/components/survey/Turnstile'

// 玩家名不再在此收集: 由问卷里绑定字段为「玩家名」的题目提供, 后端提交时从答案抽取。
// 保留两处收集入口会让二者打架 (曾出现弹窗填 1234、题目填 Shinoyuki, 加白加错人)。
interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: () => void
  submitting: boolean
  // Turnstile 相关
  turnstileEnabled?: boolean
  turnstileSiteKey?: string
  turnstileVerified?: boolean
  onTurnstileVerify?: (token: string) => void
}

export function ConfirmDialog({
  open,
  onOpenChange,
  onSubmit,
  submitting,
  turnstileEnabled = false,
  turnstileSiteKey,
  turnstileVerified = false,
  onTurnstileVerify,
}: ConfirmDialogProps) {
  // 用于强制 Turnstile 重新渲染的 key
  const [turnstileKey, setTurnstileKey] = useState(0)
  
  // 当 turnstileVerified 从 true 变为 false 时，递增 key 以触发 Turnstile 重新渲染
  useEffect(() => {
    if (!turnstileVerified && turnstileEnabled) {
      setTurnstileKey(prev => prev + 1)
    }
  }, [turnstileVerified, turnstileEnabled])

  const canSubmit = !turnstileEnabled || turnstileVerified

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md rounded-3xl">
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          transition={{ duration: 0.3, ease: [0.25, 0.46, 0.45, 0.94] }}
        >
          <DialogHeader className="text-center pb-4">
            <motion.div
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              transition={{ type: 'spring', damping: 15, delay: 0.1 }}
              className="mx-auto mb-4 w-14 h-14 rounded-2xl bg-primary/10 flex items-center justify-center"
            >
              <User className="w-7 h-7 text-primary" />
            </motion.div>
            <DialogTitle className="text-xl">确认提交</DialogTitle>
            <DialogDescription>
              请确认无误后提交，等待管理员审核
            </DialogDescription>
          </DialogHeader>

          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.2 }}
            className="space-y-4 py-4"
          >
            {/* Turnstile 验证 */}
            {turnstileEnabled && turnstileSiteKey && (
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.3 }}
                className="space-y-2"
              >
                <Label className="flex items-center gap-2">
                  <ShieldCheck className="w-4 h-4" />
                  安全验证
                </Label>
                <div className="flex justify-center">
                  {/* 当 turnstileVerified 变为 false 时，使用 key 强制 Turnstile 重新渲染 */}
                  <Turnstile
                    key={turnstileKey}
                    siteKey={turnstileSiteKey}
                    onVerify={onTurnstileVerify || (() => {})}
                    theme="auto"
                  />
                </div>
              </motion.div>
            )}
          </motion.div>

          <DialogFooter className="gap-3 sm:gap-3">
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
              className="rounded-xl flex-1"
            >
              返回修改
            </Button>
            <Button
              onClick={onSubmit}
              disabled={submitting || !canSubmit}
              className="rounded-xl flex-1"
            >
              <AnimatePresence mode="wait">
                {submitting ? (
                  <motion.div
                    key="submitting"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="flex items-center gap-2"
                  >
                    <Loader2 className="w-4 h-4 animate-spin" />
                    <span>提交中...</span>
                  </motion.div>
                ) : (
                  <motion.div
                    key="submit"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="flex items-center gap-2"
                  >
                    <span>确认提交</span>
                    <Send className="w-4 h-4" />
                  </motion.div>
                )}
              </AnimatePresence>
            </Button>
          </DialogFooter>
        </motion.div>
      </DialogContent>
    </Dialog>
  )
}
