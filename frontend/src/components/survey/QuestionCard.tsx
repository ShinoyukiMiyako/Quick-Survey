import { motion } from 'framer-motion'
import { Star } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Checkbox } from '@/components/ui/checkbox'
import { Textarea } from '@/components/ui/textarea'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import type { Question, AnswerSubmit } from '@/types/survey'
import { ImageUploader } from './ImageUploader'

// 输入类题型的统一提示文案样式, 与 text 题的字数计数保持一致
const HINT_CLASS = 'text-sm text-muted-foreground mt-2'

interface QuestionCardProps {
  question: Question
  value?: AnswerSubmit['content']
  onChange: (content: AnswerSubmit['content']) => void
  index: number
}

export function QuestionCard({ question, value, onChange, index }: QuestionCardProps) {
  // 分节说明块: 不收答案, 渲染成一页章节引导。走独立分支而不是塞进 renderQuestionContent,
  // 是因为它连"第 N 题 / 必填"这些题目外壳都不该有。
  if (question.type === 'section') {
    return (
      <Card className="rounded-3xl border-border/50 shadow-lg overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-primary/5 via-transparent to-transparent pointer-events-none" />
        <CardHeader className="relative pb-4">
          <Badge variant="secondary" className="w-fit rounded-full px-3 text-xs font-medium">
            本节说明
          </Badge>
          <CardTitle className="mt-3 text-2xl leading-relaxed">{question.title}</CardTitle>
        </CardHeader>
        {question.description ? (
          <CardContent className="relative pb-8 pt-0">
            {/* 说明里常有换行分点, 保留原样排版 */}
            <p className="whitespace-pre-wrap text-base leading-relaxed text-muted-foreground">
              {question.description}
            </p>
          </CardContent>
        ) : null}
      </Card>
    )
  }

  const renderQuestionContent = () => {
    switch (question.type) {
      case 'single':
        return (
          <RadioGroup
            value={value?.value as string || ''}
            onValueChange={(v) => onChange({ value: v })}
            className="space-y-3"
          >
            {question.options?.map((option, i) => (
              <motion.div
                key={option.value}
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.1 + i * 0.05 }}
              >
                <Label
                  htmlFor={`option-${option.value}`}
                  className="flex items-center space-x-3 p-4 rounded-2xl border border-border/50 cursor-pointer transition-all duration-200 hover:bg-accent/50 hover:border-primary/30 has-[input:checked]:bg-primary/5 has-[input:checked]:border-primary/50"
                >
                  <RadioGroupItem value={option.value} id={`option-${option.value}`} />
                  <span className="flex-1 text-base">{option.label}</span>
                </Label>
              </motion.div>
            ))}
          </RadioGroup>
        )

      case 'select': {
        const selected = typeof value?.value === 'string' ? value.value : ''
        return (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <Select
              value={selected || undefined}
              onValueChange={(v) => onChange({ value: v })}
            >
              <SelectTrigger>
                <SelectValue placeholder="请选择..." />
              </SelectTrigger>
              <SelectContent>
                {question.options?.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className={cn(HINT_CLASS, 'flex items-center justify-between gap-3')}>
              <span>共 {question.options?.length ?? 0} 个选项，请选择其一</span>
              {/* 原生 select 靠"请选择..."那一项退回未答, Radix 的面板里没有空值项,
                  选填题误选后必须另给一条清空的路, 否则只能刷新页面重填。 */}
              {!question.is_required && selected ? (
                <button
                  type="button"
                  onClick={() => onChange({})}
                  className="text-muted-foreground hover:text-foreground shrink-0 underline underline-offset-4 transition-colors"
                >
                  清除选择
                </button>
              ) : null}
            </div>
          </motion.div>
        )
      }

      case 'multiple': {
        const selectedValues = (value?.values as string[]) || []
        return (
          <div className="space-y-3">
            {question.options?.map((option, i) => (
              <motion.div
                key={option.value}
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.1 + i * 0.05 }}
              >
                <Label
                  htmlFor={`option-${option.value}`}
                  className="flex items-center space-x-3 p-4 rounded-2xl border border-border/50 cursor-pointer transition-all duration-200 hover:bg-accent/50 hover:border-primary/30 has-[input:checked]:bg-primary/5 has-[input:checked]:border-primary/50"
                >
                  <Checkbox
                    id={`option-${option.value}`}
                    checked={selectedValues.includes(option.value)}
                    onCheckedChange={(checked) => {
                      const newValues = checked
                        ? [...selectedValues, option.value]
                        : selectedValues.filter((v) => v !== option.value)
                      onChange({ values: newValues })
                    }}
                  />
                  <span className="flex-1 text-base">{option.label}</span>
                </Label>
              </motion.div>
            ))}
          </div>
        )
      }

      case 'boolean': {
        // 判断题：类似单选题，提供"是/否"两个选项
        const boolValue = value?.value as boolean | undefined
        return (
          <RadioGroup
            value={boolValue === undefined ? '' : boolValue ? 'true' : 'false'}
            onValueChange={(v) => onChange({ value: v === 'true' })}
            className="space-y-3"
          >
            {[
              { value: 'true', label: '是' },
              { value: 'false', label: '否' },
            ].map((option, i) => (
              <motion.div
                key={option.value}
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.1 + i * 0.05 }}
              >
                <Label
                  htmlFor={`boolean-${option.value}`}
                  className="flex items-center space-x-3 p-4 rounded-2xl border border-border/50 cursor-pointer transition-all duration-200 hover:bg-accent/50 hover:border-primary/30 has-[input:checked]:bg-primary/5 has-[input:checked]:border-primary/50"
                >
                  <RadioGroupItem value={option.value} id={`boolean-${option.value}`} />
                  <span className="flex-1 text-base">{option.label}</span>
                </Label>
              </motion.div>
            ))}
          </RadioGroup>
        )
      }

      case 'text':
        return (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <Textarea
              placeholder="请输入您的回答..."
              value={(value?.text as string) || ''}
              onChange={(e) => onChange({ text: e.target.value })}
              className="min-h-32 rounded-2xl resize-none text-base"
              maxLength={question.validation?.max_length}
            />
            {question.validation?.max_length && (
              <p className="text-sm text-muted-foreground mt-2 text-right">
                {((value?.text as string) || '').length} / {question.validation.max_length}
              </p>
            )}
          </motion.div>
        )

      case 'short_text': {
        const shortText = typeof value?.text === 'string' ? value.text : ''
        const maxLength = question.validation?.max_length
        return (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <Input
              placeholder="请输入您的回答..."
              value={shortText}
              onChange={(e) => onChange({ text: e.target.value })}
              className="h-12 rounded-2xl text-base"
              maxLength={maxLength}
            />
            {maxLength ? (
              <p className={`${HINT_CLASS} text-right`}>
                {shortText.length} / {maxLength}
              </p>
            ) : (
              <p className={HINT_CLASS}>请填写一行内容，不要换行</p>
            )}
          </motion.div>
        )
      }

      case 'number': {
        const minValue = question.validation?.min_value
        const maxValue = question.validation?.max_value
        const numberText =
          value?.value === undefined || value?.value === null ? '' : String(value.value)
        const rangeHint =
          minValue !== undefined && maxValue !== undefined
            ? `请填写 ${minValue} - ${maxValue} 之间的数字`
            : minValue !== undefined
              ? `不小于 ${minValue}`
              : maxValue !== undefined
                ? `不大于 ${maxValue}`
                : '请填写数字，支持小数'
        return (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <Input
              type="number"
              inputMode="decimal"
              placeholder="请输入数字"
              value={numberText}
              min={minValue}
              max={maxValue}
              onChange={(e) => {
                const raw = e.target.value
                const parsed = Number(raw)
                // 清空必须落成 {} 而非 0, 否则必填校验会把空输入误判为已答
                onChange(raw === '' || Number.isNaN(parsed) ? {} : { value: parsed })
              }}
              className="h-12 rounded-2xl text-base"
            />
            <p className={HINT_CLASS}>{rangeHint}</p>
          </motion.div>
        )
      }

      case 'date': {
        const minDate = question.validation?.min_date
        const maxDate = question.validation?.max_date
        const dateValue = typeof value?.value === 'string' ? value.value : ''
        const rangeHint =
          minDate && maxDate
            ? `可选范围 ${minDate} 至 ${maxDate}`
            : minDate
              ? `不早于 ${minDate}`
              : maxDate
                ? `不晚于 ${maxDate}`
                : '请选择日期'
        return (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <Input
              type="date"
              value={dateValue}
              min={minDate}
              max={maxDate}
              onChange={(e) => onChange(e.target.value === '' ? {} : { value: e.target.value })}
              className="h-12 rounded-2xl text-base"
            />
            <p className={HINT_CLASS}>{rangeHint}</p>
          </motion.div>
        )
      }

      case 'rating': {
        const maxRating = question.validation?.max_rating || 5
        const current = typeof value?.value === 'number' ? value.value : 0
        return (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <div className="flex flex-wrap items-center gap-2">
              {Array.from({ length: maxRating }, (_, i) => i + 1).map((score, i) => (
                <motion.button
                  key={score}
                  type="button"
                  aria-label={`${score} 分`}
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.1 + i * 0.05 }}
                  onClick={() => onChange({ value: score })}
                  className={cn(
                    'flex h-12 w-12 items-center justify-center rounded-2xl border transition-all duration-200',
                    score <= current
                      ? 'border-primary/50 bg-primary/5 text-primary'
                      : 'border-border/50 text-muted-foreground hover:border-primary/30 hover:bg-accent/50',
                  )}
                >
                  <Star className={cn('w-5 h-5', score <= current && 'fill-current')} />
                </motion.button>
              ))}
            </div>
            <p className={HINT_CLASS}>
              {current > 0 ? `已选 ${current} / ${maxRating} 分` : `请评分，1 - ${maxRating} 分`}
            </p>
          </motion.div>
        )
      }

      case 'image':
        return (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <ImageUploader
              value={(value?.images as string[]) || []}
              onChange={(images: string[]) => onChange({ images })}
              maxImages={question.validation?.max_images || 5}
            />
          </motion.div>
        )

      default:
        return <p className="text-muted-foreground">不支持的题目类型</p>
    }
  }

  return (
    <Card className="rounded-3xl border-border/50 shadow-lg overflow-hidden">
      <div className="absolute inset-0 bg-gradient-to-br from-primary/5 via-transparent to-transparent pointer-events-none" />
      
      <CardHeader className="relative pb-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1">
            <div className="flex items-center gap-2 mb-2">
              <Badge 
                variant="secondary" 
                className="rounded-full px-3 text-xs font-medium"
              >
                第 {index + 1} 题
              </Badge>
              {question.is_required && (
                <Badge variant="destructive" className="rounded-full px-2 text-xs">
                  必填
                </Badge>
              )}
            </div>
            <CardTitle className="text-xl leading-relaxed">
              {question.title}
            </CardTitle>
            {question.description && (
              <CardDescription className="mt-2 text-base">
                {question.description}
              </CardDescription>
            )}
          </div>
        </div>
      </CardHeader>

      <CardContent className="relative pt-2 pb-6">
        {renderQuestionContent()}
      </CardContent>
    </Card>
  )
}
