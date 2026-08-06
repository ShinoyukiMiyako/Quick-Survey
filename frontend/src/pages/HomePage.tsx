import { useState, useEffect, useMemo } from 'react'
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { FileText, ArrowRight, AlertCircle, Search, Clock, ListChecks, Pin, Lock } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { listSurveys } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { AvailabilityState, SurveyListItem } from '@/types/survey'
import { QueryDialog } from '@/components/survey/QueryDialog'

// 不可填状态的卡片徽标文案。后端列表接口只会给出 not_started/ended/full,
// 其余两项是为了覆盖类型全集, 免得将来放开过滤时漏渲染
const AVAILABILITY_BADGES: Record<Exclude<AvailabilityState, 'open'>, string> = {
  inactive: '已停用',
  unpublished: '未发布',
  not_started: '未开始',
  ended: '已截止',
  full: '名额已满',
}

export function HomePage() {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [surveys, setSurveys] = useState<SurveyListItem[]>([])
  const [keyword, setKeyword] = useState('')
  const [queryOpen, setQueryOpen] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        setLoading(true)
        const list = await listSurveys()
        if (!alive) return
        setSurveys(list)
        setError(null)
      } catch (err) {
        if (!alive) return
        setError(err instanceof Error ? err.message : '加载问卷列表失败')
      } finally {
        if (alive) setLoading(false)
      }
    }
    load()
    return () => {
      alive = false
    }
  }, [])

  // 卷数通常不多, 直接前端过滤标题/简介
  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return surveys
    return surveys.filter(
      (s) =>
        s.title.toLowerCase().includes(kw) ||
        (s.description ?? '').toLowerCase().includes(kw) ||
        (s.summary ?? '').toLowerCase().includes(kw)
    )
  }, [surveys, keyword])

  return (
    <div className="flex-1 w-full max-w-5xl mx-auto px-4 py-10">
      {/* 头部 + 搜索 + 查询进度入口 */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="mb-8 space-y-4"
      >
        <div className="space-y-1.5">
          <h1 className="text-2xl font-bold tracking-tight">选择一份问卷开始填写</h1>
          <p className="text-muted-foreground text-sm">点击卡片进入对应问卷；填写后可凭凭据查询审核进度。</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="relative flex-1 max-w-sm">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="搜索问卷标题 / 简介"
              className="pl-9"
            />
          </div>
          <button
            type="button"
            onClick={() => setQueryOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-primary"
          >
            <Search className="w-4 h-4" />
            <span>查询审核进度</span>
          </button>
        </div>
      </motion.div>

      {/* 加载骨架 / 错误 / 空 / 列表 */}
      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Card key={i} className="rounded-2xl overflow-hidden">
              <CardContent className="p-5 space-y-3">
                <Skeleton className="h-10 w-10 rounded-xl" />
                <Skeleton className="h-5 w-3/4" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-2/3" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : error ? (
        <div className="flex flex-col items-center justify-center gap-3 py-20 text-muted-foreground">
          <AlertCircle className="w-8 h-8" />
          <p>{error}</p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-3 py-20 text-muted-foreground">
          <FileText className="w-8 h-8" />
          <p>{keyword ? '没有匹配的问卷' : '暂无可用问卷'}</p>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((s, i) => {
            // 不可填的卷仍然列出但禁点; 口令卷只是加徽标, 仍要能点进去输口令
            const closedLabel =
              s.availability.state === 'open' ? null : AVAILABILITY_BADGES[s.availability.state]
            return (
              <motion.button
                key={s.code}
                type="button"
                disabled={closedLabel !== null}
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.35, delay: Math.min(i * 0.05, 0.3) }}
                onClick={() => navigate(`/survey/${s.code}`)}
                className={cn('group text-left', closedLabel !== null && 'cursor-not-allowed opacity-60')}
              >
                <Card className="h-full overflow-hidden rounded-2xl border-border/60 transition-all group-hover:border-primary/50 group-hover:shadow-md">
                  {s.cover_url ? (
                    <div className="h-28 w-full overflow-hidden bg-muted">
                      <img src={s.cover_url} alt="" className="h-full w-full object-cover" />
                    </div>
                  ) : null}
                  <CardContent className="space-y-3 p-5">
                    <div className="flex items-start justify-between gap-2">
                      <div
                        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-lg"
                        style={
                          s.theme_color
                            ? { backgroundColor: `${s.theme_color}1a`, color: s.theme_color }
                            : undefined
                        }
                      >
                        {s.icon ? (
                          <span>{s.icon}</span>
                        ) : (
                          <FileText className={s.theme_color ? 'h-5 w-5' : 'h-5 w-5 text-primary'} />
                        )}
                      </div>
                      <div className="flex flex-wrap items-center justify-end gap-1.5">
                        {s.is_pinned ? (
                          <Badge variant="secondary" className="gap-1">
                            <Pin className="h-3 w-3" />
                            置顶
                          </Badge>
                        ) : null}
                        {s.locked ? (
                          <Badge variant="secondary" className="gap-1">
                            <Lock className="h-3 w-3" />
                            需口令
                          </Badge>
                        ) : null}
                        {closedLabel ? (
                          <Badge variant="outline" className="text-muted-foreground">
                            {closedLabel}
                          </Badge>
                        ) : null}
                      </div>
                    </div>
                    <div className="space-y-1">
                      <h3 className="font-semibold leading-snug line-clamp-2">{s.title}</h3>
                      {s.summary || s.description ? (
                        <p className="line-clamp-2 text-sm text-muted-foreground">{s.summary || s.description}</p>
                      ) : null}
                    </div>
                    <div className="flex items-center gap-3 pt-1 text-xs text-muted-foreground">
                      <span className="inline-flex items-center gap-1">
                        <ListChecks className="h-3.5 w-3.5" />
                        {s.question_count} 题
                      </span>
                      {s.estimated_minutes ? (
                        <span className="inline-flex items-center gap-1">
                          <Clock className="h-3.5 w-3.5" />约 {s.estimated_minutes} 分钟
                        </span>
                      ) : null}
                      <ArrowRight className="ml-auto h-4 w-4 opacity-0 transition-opacity group-hover:opacity-100" />
                    </div>
                  </CardContent>
                </Card>
              </motion.button>
            )
          })}
        </div>
      )}

      <QueryDialog open={queryOpen} onOpenChange={setQueryOpen} />
    </div>
  )
}
