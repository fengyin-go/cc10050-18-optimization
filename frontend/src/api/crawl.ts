/** 采集调度轮次接口：暂停、续跑、签收、回执与交付校验都走这一组专用端点。 */
import { request } from './client'

export type Eligibility = {
  state: '收录' | '暂挂' | '需要恢复'
  eligible: boolean
  round_id: string
  round_status: string
  page_no: string
  delivery_status: string
  stored: boolean
  stored_sample_id: number | null
  receipts: string
  received: number
  expected: number
  signed: boolean
  reasons: string[]
  checked_at?: string
}

export type PageOperation = {
  sequence: number
  page_no: string
  page_status: string
  receipt_order: number | null
  receipts: string
  received: number
  expected: number
  signed: boolean
  stored_sample_id: number | null
  eligibility: Eligibility['state']
  eligible: boolean
  reasons: string[]
  available_actions: string[]
  order_basis: string
}

export type RoundPage = {
  page_no: string
  delivery_status: string
  signed: boolean
  signed_at: string | null
  receipts: string
  received: number
  expected: number
  stored_sample_id: number | null
  stored_at: string | null
  transfer_started_at: string | null
  inflight: boolean
}

export type RoundEvent = {
  order: number
  kind: string
  page_no: string | null
  at: string
  note: string
}

export type RoundView = {
  round_id: string
  round_seq: number
  status: string
  opened_at: string
  paused_at: string | null
  resumed_at: string | null
  completed_at: string | null
  pause_landing: number | null
  pages: RoundPage[]
  page_count: number
  signed_pages: number
  stored_pages: number
  pending_receipts: number
  has_pause_receipt: boolean
  has_completion_receipt: boolean
  events: RoundEvent[]
}

export type PauseReceipt = {
  kind: string
  plan_id: number
  plan_name: string
  round_id: string
  paused_at: string | null
  pause_landing: number | null
  signed_pages: {
    page_no: string
    signed_at: string | null
    stored_sample_id: number | null
    kept_in_round: string
  }[]
  inflight_pages: {
    page_no: string
    delivery_status: string
    transfer_started_at: string | null
    receipts: string
    message: string
  }[]
  pending_receipts: { page_no: string; receipts: string; awaiting: number }[]
  sample_check: SampleCheck
  next_round_blocked: boolean
}

export type SampleCheck = {
  round_id: string | null
  stored_pages: { page_no: string; sample_id: number; stored_at: string | null }[]
  missing_samples: { page_no: string; sample_id: number }[]
  sample_count: number
  samples_after_pause: { sample_id: number; page_no: string; stored_at: string }[]
  intact: boolean
}

export type CompletionReceipt = {
  kind: string
  round_id: string
  round_seq: number
  completed_at: string
  ok: boolean
  outcome: string
  pages: { page_no: string; signed: boolean; stored_sample_id: number | null; receipts: string }[]
  sample_check: SampleCheck
}

export type PlanRow = {
  id: number
  status: string
  [field: string]: unknown
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  return (await response.json()) as T
}

async function get<T>(path: string): Promise<T> {
  const response = await request(path)
  return (await response.json()) as T
}

export const listRounds = (planId: number) =>
  get<{ ok: boolean; message: string; rounds: RoundView[]; plan?: Record<string, unknown> }>(
    `/api/crawl_schedule/${planId}/rounds`,
  )

export const roundDetail = (planId: number, roundId: string) =>
  get<{
    ok: boolean
    message: string
    round: RoundView
    eligibility: Eligibility[]
    summary: Record<string, number>
    page_operations: PageOperation[]
    sample_check: SampleCheck
    pause_receipt: PauseReceipt | null
    completion_receipt: CompletionReceipt | null
  }>(`/api/crawl_schedule/${planId}/rounds/${roundId}`)

export const signoffDetail = (planId: number, roundId: string, pageNo: string) =>
  get<{
    ok: boolean
    message: string
    round_id: string
    round_status: string
    paused: boolean
    page: {
      page_no: string
      delivery_status: string
      transfer_started_at: string | null
      signed_at: string | null
      signed_by: string | null
      stored_sample_id: number | null
      stored_at: string | null
      expected_receipts: number
      received_receipts: number
      receipts: { no: number; at: string; kind: string; order: number; note: string }[]
      inflight: boolean
    }
    eligibility: Eligibility
    available_actions: string[]
    sequence: number | null
    order_basis: string
    pause_landing: number | null
    refresh_hint: string
  }>(`/api/crawl_schedule/${planId}/rounds/${roundId}/pages/${pageNo}`)

export const pausePlan = (planId: number, reason: string, roundId?: string) =>
  post<{
    ok: boolean
    message: string
    needs_confirmation?: boolean
    conflicts?: PauseReceipt['inflight_pages']
    pause_landing?: number
    pause_receipt?: PauseReceipt
  }>(`/api/crawl_schedule/${planId}/pause`, { reason, round_id: roundId ?? null })

export const confirmPause = (planId: number, roundId?: string) =>
  post<{ ok: boolean; message: string; pause_landing?: number }>(
    `/api/crawl_schedule/${planId}/confirm-pause`, { round_id: roundId ?? null },
  )

export const resumePlan = (planId: number, roundId?: string) =>
  post<{
    ok: boolean
    message: string
    round_id?: string
    eligibility?: Eligibility[]
    summary?: Record<string, number>
    page_operations?: PageOperation[]
    resume_receipt?: { sample_check: SampleCheck; summary: Record<string, number>; paused_round_id?: string }
  }>(`/api/crawl_schedule/${planId}/resume`, { round_id: roundId ?? null })

export const openRound = (planId: number) =>
  post<{ ok: boolean; message: string; round?: RoundView }>(`/api/crawl_schedule/${planId}/rounds`)

export const requestRound = (planId: number, source = '调度队列') =>
  post<{ ok: boolean; accepted?: boolean; paused?: boolean; message: string }>(
    `/api/crawl_schedule/${planId}/round-request?source=${encodeURIComponent(source)}`,
  )

export const completeRound = (planId: number, roundId?: string) =>
  post<{
    ok: boolean
    message: string
    pending_pages?: string[]
    completion_receipt?: CompletionReceipt
  }>(`/api/crawl_schedule/${planId}/complete`, { round_id: roundId ?? null })

export const getCompletionReceipt = (planId: number, roundId?: string) =>
  get<{ ok: boolean; message: string; completion_receipt?: CompletionReceipt }>(
    `/api/crawl_schedule/${planId}/completion-receipt${roundId ? `?round_id=${encodeURIComponent(roundId)}` : ''}`,
  )

export const signPage = (planId: number, roundId: string, pageNo: string, note = '') =>
  post<{ ok: boolean; message: string; stored_sample_id?: number; eligibility?: Eligibility }>(
    `/api/crawl_schedule/${planId}/signoff`,
    { round_id: roundId, page_no: pageNo, note },
  )

export const recordReceipt = (planId: number, roundId: string, pageNo: string, note = '') =>
  post<{ ok: boolean; message: string; receipts?: string; awaiting?: number; eligibility?: Eligibility }>(
    `/api/crawl_schedule/${planId}/receipt`,
    { round_id: roundId, page_no: pageNo, note },
  )

export const verifyDeliveries = (planId: number, roundId?: string) =>
  post<{
    ok: boolean
    message: string
    round_id?: string
    summary?: Record<string, number>
    pages?: Eligibility[]
  }>(`/api/crawl_schedule/${planId}/verify-deliveries`, { round_id: roundId ?? null })

export const taskEligibility = (taskId: number, pageNo: string, roundId?: string) =>
  post<{ ok: boolean; message: string } & Eligibility>(
    `/api/price_crawler/${taskId}/verify-delivery`,
    { task_id: taskId, page_no: pageNo, round_id: roundId ?? null },
  )

export type DeliveryOutcome = Eligibility & {
  outcome: '收录' | '暂挂' | '需要恢复'
  delivered: boolean
  handoff: string
  task: string
}

export const taskOperations = (taskId: number, roundId?: string) =>
  get<{
    ok: boolean
    message: string
    round_id?: string
    round_status?: string
    operations?: PageOperation[]
    summary?: Record<string, number>
  }>(`/api/price_crawler/${taskId}/operations${roundId ? `?round_id=${encodeURIComponent(roundId)}` : ''}`)

export const deliverTaskPages = (taskId: number, roundId?: string, pageNo?: string) =>
  post<{
    ok: boolean
    message: string
    round_id?: string
    source?: string
    checked_at?: string
    summary?: Record<string, number>
    results?: DeliveryOutcome[]
    page_operations?: PageOperation[]
  }>(`/api/price_crawler/${taskId}/deliver-page`, {
    task_id: taskId,
    round_id: roundId ?? null,
    page_no: pageNo ?? null,
  })
