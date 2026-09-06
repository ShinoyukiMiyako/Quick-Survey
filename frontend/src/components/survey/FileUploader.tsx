import { useCallback, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Upload, X, Loader2, FileArchive, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { uploadAttachment } from '@/lib/api'
import { toast } from 'sonner'
import type { UploadedAttachment } from '@/types/survey'

interface FileUploaderProps {
  value: UploadedAttachment[]
  onChange: (files: UploadedAttachment[]) => void
  maxFiles?: number
  /** 题目配置的扩展名白名单 (小写带点); 为空表示只受站点级白名单约束 */
  allowedExtensions?: string[]
  /** 单个附件体积上限 (MB), 与后端 upload.max_file_size_mb 对齐 */
  maxSizeMb?: number
}

/** 字节数压成人看得懂的体积 */
function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}

/** 取文件名的扩展名 (小写带点); 无扩展名返回空串 */
function fileExtension(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot).toLowerCase() : ''
}

export function FileUploader({
  value,
  onChange,
  maxFiles = 3,
  allowedExtensions = [],
  maxSizeMb = 50,
}: FileUploaderProps) {
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)

  const accept = allowedExtensions.join(',')
  const extHint = allowedExtensions.length > 0 ? allowedExtensions.join(' / ') : '常见附件格式'

  const handleFileSelect = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return

      const remainingSlots = maxFiles - value.length
      if (remainingSlots <= 0) {
        toast.error(`最多只能上传 ${maxFiles} 个文件`)
        return
      }

      const picked = Array.from(files).slice(0, remainingSlots)

      // 先整批体检再上传: 一半传上去另一半被拒会让玩家搞不清到底成了几个
      for (const file of picked) {
        if (allowedExtensions.length > 0 && !allowedExtensions.includes(fileExtension(file.name))) {
          toast.error(`「${file.name}」格式不符合要求，仅接受 ${extHint}`)
          return
        }
        if (file.size > maxSizeMb * 1024 * 1024) {
          toast.error(`「${file.name}」超过 ${maxSizeMb}MB 上限`)
          return
        }
        if (file.size === 0) {
          toast.error(`「${file.name}」是空文件`)
          return
        }
      }

      setUploading(true)
      const uploaded: UploadedAttachment[] = []
      try {
        for (const file of picked) {
          const result = await uploadAttachment(file)
          // 原始文件名随答案一起存: 落盘名是 uuid, 审核端只看存储名什么也看不出来
          uploaded.push({ url: result.url, name: result.filename || file.name, size: result.size })
        }
        onChange([...value, ...uploaded])
        toast.success(`成功上传 ${uploaded.length} 个文件`)
      } catch (err) {
        toast.error(err instanceof Error ? err.message : '上传失败，请重试')
      } finally {
        setUploading(false)
      }
    },
    [value, maxFiles, allowedExtensions, extHint, maxSizeMb, onChange],
  )

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      setDragOver(false)
      handleFileSelect(e.dataTransfer.files)
    },
    [handleFileSelect],
  )

  const handleRemove = useCallback(
    (index: number) => {
      onChange(value.filter((_, i) => i !== index))
    },
    [value, onChange],
  )

  const canUploadMore = value.length < maxFiles

  return (
    <div className="space-y-4">
      <AnimatePresence mode="popLayout">
        {value.length > 0 && (
          <motion.ul
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="space-y-2"
          >
            {value.map((item, index) => (
              <motion.li
                key={item.url}
                layout
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.96 }}
                transition={{ duration: 0.2 }}
                className="flex items-center gap-3 rounded-2xl border border-border/50 bg-card/50 p-3"
              >
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/10">
                  <FileArchive className="h-5 w-5 text-primary" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium" title={item.name}>
                    {item.name}
                  </p>
                  <p className="text-xs text-muted-foreground">{formatSize(item.size)}</p>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="rounded-full text-muted-foreground hover:text-destructive"
                  aria-label={`移除 ${item.name}`}
                  onClick={() => handleRemove(index)}
                >
                  <X className="h-4 w-4" />
                </Button>
              </motion.li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>

      {canUploadMore && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          onDrop={handleDrop}
          onDragOver={(e) => {
            e.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={(e) => {
            e.preventDefault()
            setDragOver(false)
          }}
          className={`relative rounded-2xl border-2 border-dashed p-6 text-center transition-all duration-200 ${
            dragOver
              ? 'border-primary bg-primary/5'
              : 'border-border/50 hover:border-primary/30 hover:bg-accent/30'
          }`}
        >
          <input
            type="file"
            accept={accept || undefined}
            multiple={maxFiles > 1}
            onChange={(e) => {
              handleFileSelect(e.target.files)
              // 清空原生 value: 不清的话删掉后再选同一个文件不触发 change
              e.target.value = ''
            }}
            className="absolute inset-0 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
            disabled={uploading}
            aria-label="选择要上传的文件"
          />

          <AnimatePresence mode="wait">
            {uploading ? (
              <motion.div
                key="uploading"
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.9 }}
                className="flex flex-col items-center gap-3"
              >
                <Loader2 className="h-10 w-10 animate-spin text-primary" />
                <p className="text-muted-foreground">上传中，大文件请勿关闭页面…</p>
              </motion.div>
            ) : (
              <motion.div
                key="idle"
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.9 }}
                className="flex flex-col items-center gap-3"
              >
                <motion.div
                  animate={dragOver ? { scale: 1.1 } : { scale: 1 }}
                  className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10"
                >
                  {dragOver ? (
                    <Upload className="h-7 w-7 text-primary" />
                  ) : value.length > 0 ? (
                    <Plus className="h-7 w-7 text-primary" />
                  ) : (
                    <FileArchive className="h-7 w-7 text-primary" />
                  )}
                </motion.div>
                <div>
                  <p className="font-medium">{dragOver ? '释放以上传文件' : '点击或拖拽上传文件'}</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    支持 {extHint}，单个最大 {maxSizeMb}MB，最多 {maxFiles} 个
                  </p>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>
      )}

      <p className="text-center text-sm text-muted-foreground">
        已上传 {value.length} / {maxFiles} 个文件
      </p>
    </div>
  )
}
