import axios, { AxiosError } from 'axios'
import type {
  ApiResponse,
  PublicSurvey,
  SurveyListItem,
  SubmissionCreate,
  SecurityConfig,
  UploadResponse,
  SubmitResult,
  SubmissionStatus,
  RegistrationCodeResult,
} from '@/types/survey'

// 问卷后端基址。生产留空 -> 同源相对 (/api/v1, /uploads); 开发用 .env 的 VITE_API_URL 指向 localhost:8000。
const API_BASE = import.meta.env.VITE_API_URL || ''

const http = axios.create({
  baseURL: `${API_BASE}/api/v1`,
  timeout: 20000,
})

// 把后端错误统一转成带可读 message 的 Error 抛出:
// - HTTPException 走 FastAPI 默认格式 { detail }
// - 业务包装失败走 ApiResponse 的 error.message
function toError(err: unknown, fallback: string): Error {
  if (err instanceof AxiosError) {
    const body = err.response?.data as { detail?: string; error?: { message?: string } } | undefined
    const msg = body?.detail || body?.error?.message
    if (msg) return new Error(msg)
  }
  if (err instanceof Error) return err
  return new Error(fallback)
}

// 解包 ApiResponse; success=false 或 data 缺失也视为失败抛错, 不静默吞
function unwrap<T>(resp: ApiResponse<T>, fallback: string): T {
  if (!resp.success || resp.data === undefined) {
    throw new Error(resp.error?.message || fallback)
  }
  return resp.data
}

export async function getSurveyByCode(code: string): Promise<PublicSurvey> {
  try {
    const { data } = await http.get<ApiResponse<PublicSurvey>>(`/public/surveys/${encodeURIComponent(code)}`)
    return unwrap(data, '问卷不存在或已关闭')
  } catch (err) {
    throw toError(err, '问卷不存在或已关闭')
  }
}

// 注意: /public/survey/active 是旧的单激活卷入口, 后端未随门户改造下发 availability/locked 等字段。
// 门户与答题页一律走 getSurveyByCode, 这里仅为兼容历史调用保留。
export async function getActiveSurvey(): Promise<PublicSurvey> {
  try {
    const { data } = await http.get<ApiResponse<PublicSurvey>>('/public/survey/active')
    return unwrap(data, '当前没有可用的问卷')
  } catch (err) {
    throw toError(err, '当前没有可用的问卷')
  }
}

// 门户可选问卷列表 (轻量摘要, 已按 置顶 > 排序位 > 时间 排好序)
export async function listSurveys(category?: string): Promise<SurveyListItem[]> {
  try {
    const { data } = await http.get<ApiResponse<{ surveys: SurveyListItem[] }>>('/public/surveys', {
      params: category ? { category } : undefined,
    })
    return unwrap(data, '加载问卷列表失败').surveys
  } catch (err) {
    throw toError(err, '加载问卷列表失败')
  }
}

// 口令卷解锁: 校验通过后后端返回带 questions 的完整问卷 (locked 已变为 false)。
// 该端点有 IP 限流, 失败 (403) 的文案直接透传给玩家。
export async function unlockSurvey(code: string, password: string): Promise<PublicSurvey> {
  try {
    const { data } = await http.post<ApiResponse<PublicSurvey>>(
      `/public/surveys/${encodeURIComponent(code)}/unlock`,
      { password },
    )
    return unwrap(data, '访问口令不正确')
  } catch (err) {
    throw toError(err, '访问口令不正确')
  }
}

export async function submitSurvey(
  code: string,
  payload: SubmissionCreate,
): Promise<SubmitResult> {
  try {
    const { data } = await http.post<ApiResponse<SubmitResult>>(
      `/public/surveys/${encodeURIComponent(code)}/submit`,
      payload,
    )
    return unwrap(data, '提交失败，请稍后重试')
  } catch (err) {
    throw toError(err, '提交失败，请稍后重试')
  }
}

export async function getSecurityConfig(): Promise<SecurityConfig> {
  try {
    const { data } = await http.get<ApiResponse<SecurityConfig>>('/public/security-config')
    return unwrap(data, '获取安全配置失败')
  } catch (err) {
    throw toError(err, '获取安全配置失败')
  }
}

// 凭 token 查询单条提交状态 (取代旧的按玩家名查询)
export async function querySubmissionStatus(token: string): Promise<SubmissionStatus> {
  try {
    const { data } = await http.get<ApiResponse<{ submission: SubmissionStatus }>>('/public/submissions/query', {
      params: { token },
    })
    return unwrap(data, '未找到相关的问卷提交记录').submission
  } catch (err) {
    throw toError(err, '未找到相关的问卷提交记录')
  }
}

// 凭 token 自助领取注册码 (仅通过审核且未领取过时返回明文码)
export async function redeemRegistrationCode(token: string): Promise<RegistrationCodeResult> {
  try {
    const { data } = await http.post<ApiResponse<RegistrationCodeResult>>(
      `/public/submissions/${encodeURIComponent(token)}/registration-code`,
    )
    return unwrap(data, '领取注册码失败，请稍后重试')
  } catch (err) {
    throw toError(err, '领取注册码失败，请稍后重试')
  }
}

export async function uploadImage(file: File): Promise<UploadResponse> {
  const form = new FormData()
  form.append('file', file)
  try {
    const { data } = await http.post<ApiResponse<UploadResponse>>('/public/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      // 上传走慢速移动网络, 覆盖全局 20s 超时 (压缩后通常很快, 留足余量防弱网中断)
      timeout: 120000,
    })
    return unwrap(data, '上传失败，请重试')
  } catch (err) {
    throw toError(err, '上传失败，请重试')
  }
}

// 后端返回的图片地址为相对路径 (/uploads/xxx), 拼成可直接用于 img src 的完整地址。
// 已是绝对 http(s) 地址则原样返回。
export function getImageUrl(url: string): string {
  if (!url) return ''
  if (/^https?:\/\//i.test(url)) return url
  return `${API_BASE}${url.startsWith('/') ? '' : '/'}${url}`
}
