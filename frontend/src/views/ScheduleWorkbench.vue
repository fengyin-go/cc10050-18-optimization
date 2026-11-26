<template>
  <section class="page" data-module="crawl_schedule">
    <header class="page-head">
      <div>
        <h2>采集调度计划</h2>
        <p class="page-desc">按轮次调度价格爬取任务；暂停冻结入口但保留已签收页面，续跑只继续被暂停的旧轮。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="reload-plans" @click="reloadPlans()">刷新计划</button>
        <button class="btn ghost" type="button" data-testid="request-round" @click="triggerRoundRequest">
          模拟队列请求新轮
        </button>
      </div>
    </header>

    <p v-if="notice" class="notice" :class="{ error: noticeIsError }" data-testid="notice">{{ notice }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">计划总数</span>
        <strong class="stat-value">{{ plans.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">暂停中计划</span>
        <strong class="stat-value" data-testid="paused-count">{{ pausedPlans }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前轮次</span>
        <strong class="stat-value" data-testid="current-round">{{ selectedPlan?.current_round_id || '—' }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>计划名称</th>
          <th>关联任务</th>
          <th>调度周期</th>
          <th>下次执行</th>
          <th>上次状态</th>
          <th>当前状态</th>
          <th>暂停轮次</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="plan in plans"
          :key="plan.id"
          :data-testid="`plan-${plan.id}`"
          :data-status="String(plan.status)"
        >
          <td>{{ plan['计划名称'] }}</td>
          <td>{{ plan['关联任务'] }}</td>
          <td>{{ plan['调度周期'] }}</td>
          <td>{{ plan['下次执行'] || '—' }}</td>
          <td>{{ plan['上次状态'] || '—' }}</td>
          <td>{{ plan.status }}{{ plan.paused ? '（已暂停）' : '' }}</td>
          <td>{{ plan.paused_round_id || '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`select-${plan.id}`" @click="selectPlan(plan)">
              查看轮次
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`pause-${plan.id}`"
              :disabled="Boolean(plan.paused)"
              @click="pauseSelected(plan)"
            >
              暂停计划
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`resume-${plan.id}`"
              :disabled="!plan.paused"
              @click="resumeSelected(plan)"
            >
              继续计划
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`confirm-${plan.id}`"
              :disabled="!plan.paused"
              @click="confirmSelected(plan)"
            >
              确认暂停
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`new-round-${plan.id}`"
              :disabled="Boolean(plan.paused)"
              @click="openNewRound(plan)"
            >
              新建轮次
            </button>
          </td>
        </tr>
        <tr v-if="!plans.length">
          <td colspan="8" class="empty-state">暂无采集调度计划数据</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selectedPlan" class="round-panel" data-testid="round-panel">
      <header class="round-head">
        <div>
          <h3>{{ selectedPlan['计划名称'] }}</h3>
          <p class="page-desc">
            关联任务 {{ selectedPlan['关联任务'] }} · 暂停落点
            {{ activeRound?.pause_landing ?? '—' }} · 事件序号 {{ eventSeq }}
          </p>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="verify-deliveries" @click="verifyAll">
            校验交付资格
          </button>
          <button class="btn" type="button" data-testid="complete-round" @click="completeCurrentRound">
            回写完成回执
          </button>
          <button class="btn" type="button" data-testid="lookup-completion" @click="lookupCompletion(activeRoundId)">
            按轮次回查完成回执
          </button>
        </div>
      </header>

      <div v-if="rounds.length" class="round-tabs">
        <button
          v-for="round in rounds"
          :key="round.round_id"
          class="round-tab"
          :class="{ active: round.round_id === activeRoundId }"
          type="button"
          :data-testid="`round-tab-${round.round_id}`"
          @click="openRoundView(round.round_id)"
        >
          {{ round.round_id }} · {{ round.status }}
        </button>
      </div>
      <p v-else class="empty-state">该计划还没有执行轮次，可用「新建轮次」开启第一轮。</p>

      <template v-if="roundDetailData">
        <ul class="event-strip" data-testid="round-events">
          <li v-for="event in roundDetailData.round.events" :key="event.order">
            <span class="event-order">#{{ event.order }}</span>
            {{ event.kind }}<template v-if="event.page_no">（{{ event.page_no }} 页）</template>
            <span class="event-note">{{ event.note }}</span>
          </li>
        </ul>

        <div class="summary-row" data-testid="round-summary">
          <span>轮次状态：{{ roundDetailData.round.status }}</span>
          <span>已签收：{{ roundDetailData.round.signed_pages }}</span>
          <span>已入库：{{ roundDetailData.round.stored_pages }}</span>
          <span>待回执页面：{{ roundDetailData.round.pending_receipts }}</span>
          <span>
            收录 {{ roundDetailData.summary['收录'] ?? 0 }} / 暂挂 {{ roundDetailData.summary['暂挂'] ?? 0 }} /
            需要恢复 {{ roundDetailData.summary['需要恢复'] ?? 0 }}
          </span>
        </div>

        <table class="data-table">
          <thead>
            <tr>
              <th>顺序</th>
              <th>页面</th>
              <th>页面状态</th>
              <th>回执</th>
              <th>签收</th>
              <th>样本</th>
              <th>交付资格</th>
              <th>可做操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="op in roundDetailData.page_operations"
              :key="op.page_no"
              :data-testid="`page-${op.page_no}`"
              :data-eligibility="op.eligibility"
            >
              <td>{{ op.sequence }}（{{ op.order_basis }}）</td>
              <td>{{ op.page_no }}</td>
              <td>{{ op.page_status }}</td>
              <td>{{ op.receipts }}</td>
              <td>{{ op.signed ? '已签收' : '待签收' }}</td>
              <td>{{ op.stored_sample_id ?? '—' }}</td>
              <td>
                <strong class="elig" :class="`elig-${op.eligibility}`">{{ op.eligibility }}</strong>
                <ul class="reason-list">
                  <li v-for="reason in op.reasons" :key="reason">{{ reason }}</li>
                </ul>
              </td>
              <td class="row-actions">
                <button
                  v-for="action in op.available_actions"
                  :key="action"
                  class="link"
                  type="button"
                  :data-testid="`page-action-${op.page_no}-${action}`"
                  @click="runPageAction(action, op.page_no)"
                >
                  {{ action }}
                </button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`signoff-${op.page_no}`"
                  @click="openSignoff(op.page_no)"
                >
                  签收详情
                </button>
              </td>
            </tr>
          </tbody>
        </table>

        <div class="receipt-grid">
          <article class="receipt-card" data-testid="pause-receipt">
            <h4>暂停回执</h4>
            <p v-if="!roundDetailData.pause_receipt" class="empty-state">本轮未暂停</p>
            <template v-else>
              <p>落点事件 #{{ roundDetailData.pause_receipt.pause_landing }}，暂停于
                {{ roundDetailData.pause_receipt.paused_at }}</p>
              <p>保留在原轮次的已签收页面：
                {{ roundDetailData.pause_receipt.signed_pages.map((p) => p.page_no).join('、') || '无' }}
              </p>
              <p v-for="item in roundDetailData.pause_receipt.inflight_pages" :key="`c-${item.page_no}`" class="warn">
                {{ item.message }}（回执 {{ item.receipts }}）
              </p>
              <p>
                样本核对：{{ roundDetailData.pause_receipt.sample_check.intact ? '已入库页面全部留库' : '存在缺失样本' }}，
                共 {{ roundDetailData.pause_receipt.sample_check.sample_count }} 份
              </p>
              <p>暂停期间新轮次调度：{{ roundDetailData.pause_receipt.next_round_blocked ? '已阻断' : '未阻断' }}</p>
            </template>
          </article>
          <article class="receipt-card" data-testid="completion-receipt">
            <h4>完成回执</h4>
            <p v-if="!roundDetailData.completion_receipt" class="empty-state">本轮未回写完成回执</p>
            <template v-else>
              <p>{{ roundDetailData.completion_receipt.outcome }} ·
                {{ roundDetailData.completion_receipt.completed_at }}</p>
              <p>轮次 {{ roundDetailData.completion_receipt.round_id }}（第
                {{ roundDetailData.completion_receipt.round_seq }} 轮）</p>
              <p>
                页面：
                {{ roundDetailData.completion_receipt.pages.map((p) => `${p.page_no}（${p.receipts}）`).join('、') }}
              </p>
              <p>样本核对：{{ roundDetailData.completion_receipt.sample_check.intact ? '完整' : '缺失' }}</p>
            </template>
          </article>
        </div>
      </template>
    </section>

    <div v-if="signoff" class="modal-backdrop" data-testid="signoff-modal">
      <div class="modal signoff-modal">
        <h3>页面 {{ signoff.page.page_no }} 签收详情</h3>
        <p class="page-desc">
          轮次 {{ signoff.round_id }} · {{ signoff.round_status }} · 暂停中：{{ signoff.paused ? '是' : '否' }}
        </p>
        <dl class="signoff-grid">
          <div><dt>页面状态</dt><dd>{{ signoff.page.delivery_status }}</dd></div>
          <div><dt>传输开始</dt><dd>{{ signoff.page.transfer_started_at || '—' }}</dd></div>
          <div><dt>签收时间</dt><dd>{{ signoff.page.signed_at || '待签收' }}</dd></div>
          <div><dt>回执进度</dt><dd>{{ signoff.page.received_receipts }}/{{ signoff.page.expected_receipts }}</dd></div>
          <div><dt>入库样本</dt><dd>{{ signoff.page.stored_sample_id ?? '—' }}</dd></div>
          <div><dt>交付资格</dt><dd>{{ signoff.eligibility.state }}</dd></div>
        </dl>
        <p v-for="reason in signoff.eligibility.reasons" :key="reason" class="reason-line">{{ reason }}</p>
        <p v-if="signoff.refresh_hint" class="warn" data-testid="refresh-hint">{{ signoff.refresh_hint }}</p>
        <p class="page-desc">可做操作：{{ signoff.available_actions.join('、') || '无' }}</p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="signoff-refresh" @click="refreshSignoff">
            刷新回执
          </button>
          <button
            class="btn"
            type="button"
            data-testid="signoff-record-receipt"
            :disabled="signoff.eligibility.received >= signoff.eligibility.expected"
            @click="recordOneReceipt"
          >
            登记一份回执
          </button>
          <button class="btn primary" type="button" data-testid="signoff-sign" @click="signPageNow">
            签收并入库
          </button>
          <button class="btn ghost" type="button" @click="signoff = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  completeRound,
  confirmPause,
  getCompletionReceipt,
  listRounds,
  openRound,
  pausePlan,
  recordReceipt,
  requestRound,
  resumePlan,
  roundDetail,
  signPage,
  signoffDetail,
  verifyDeliveries,
  type RoundView,
} from '@/api/crawl'
import { listEntries } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

type PlanEntry = EntryRow & {
  paused?: boolean
  paused_round_id?: string | null
  current_round_id?: string | null
  event_seq?: number
}

const plans = ref<PlanEntry[]>([])
const selectedPlan = ref<PlanEntry | null>(null)
const rounds = ref<RoundView[]>([])
const activeRoundId = ref('')
const roundDetailData = ref<Awaited<ReturnType<typeof roundDetail>> | null>(null)
const signoff = ref<Awaited<ReturnType<typeof signoffDetail>> | null>(null)
const notice = ref('')
const noticeIsError = ref(false)

const pausedPlans = computed(() => plans.value.filter((plan) => plan.paused).length)
const activeRound = computed(
  () => rounds.value.find((round) => round.round_id === activeRoundId.value) ?? null,
)
const eventSeq = computed(() => selectedPlan.value?.event_seq ?? 0)

function say(message: string, isError = false) {
  notice.value = message
  noticeIsError.value = isError
}

async function reloadPlans(keepSelection = true) {
  try {
    const payload = await listEntries('crawl_schedule')
    plans.value = payload.items as PlanEntry[]
    const previousId = selectedPlan.value?.id
    const match = keepSelection ? plans.value.find((plan) => plan.id === previousId) : null
    if (match) {
      selectedPlan.value = match
    }
  } catch (error) {
    say(error instanceof Error ? error.message : '计划读取失败', true)
  }
}

async function selectPlan(plan: PlanEntry) {
  selectedPlan.value = plan
  signoff.value = null
  await loadRounds()
}

async function loadRounds(preferred?: string) {
  if (!selectedPlan.value) return
  try {
    const payload = await listRounds(Number(selectedPlan.value.id))
    if (!payload.ok) throw new Error(payload.message)
    rounds.value = payload.rounds
    const current = preferred || selectedPlan.value.current_round_id || ''
    const target =
      rounds.value.find((round) => round.round_id === current) ??
      rounds.value[rounds.value.length - 1]
    if (target) {
      await openRoundView(target.round_id)
    } else {
      activeRoundId.value = ''
      roundDetailData.value = null
    }
  } catch (error) {
    say(error instanceof Error ? error.message : '轮次读取失败', true)
  }
}

async function openRoundView(roundId: string) {
  if (!selectedPlan.value) return
  activeRoundId.value = roundId
  try {
    const payload = await roundDetail(Number(selectedPlan.value.id), roundId)
    if (!payload.ok) throw new Error(payload.message)
    roundDetailData.value = payload
  } catch (error) {
    say(error instanceof Error ? error.message : '轮次详情读取失败', true)
  }
}

async function pauseSelected(plan: PlanEntry) {
  try {
    const result = await pausePlan(Number(plan.id), '值班员从计划工作台暂停')
    say(result.message, !result.ok)
    if (result.ok && result.needs_confirmation) {
      say(`${result.message}；请在值班员确认暂停状态后继续旧轮`, true)
    }
    await reloadPlans()
    selectedPlan.value = plans.value.find((p) => p.id === plan.id) ?? selectedPlan.value
    await loadRounds()
  } catch (error) {
    say(error instanceof Error ? error.message : '暂停失败', true)
  }
}

async function confirmSelected(plan: PlanEntry) {
  try {
    const result = await confirmPause(Number(plan.id))
    say(result.message, !result.ok)
    await reloadPlans()
    await loadRounds()
  } catch (error) {
    say(error instanceof Error ? error.message : '确认暂停失败', true)
  }
}

async function resumeSelected(plan: PlanEntry) {
  try {
    const result = await resumePlan(Number(plan.id))
    if (!result.ok) {
      say(result.message, true)
      return
    }
    const summary = result.summary ?? {}
    say(
      `${result.message}；收录 ${summary['收录'] ?? 0} / 暂挂 ${summary['暂挂'] ?? 0} / ` +
      `需要恢复 ${summary['需要恢复'] ?? 0}`,
    )
    await reloadPlans()
    selectedPlan.value = plans.value.find((p) => p.id === plan.id) ?? selectedPlan.value
    await loadRounds(result.round_id)
  } catch (error) {
    say(error instanceof Error ? error.message : '续跑失败', true)
  }
}

async function openNewRound(plan: PlanEntry) {
  try {
    const result = await openRound(Number(plan.id))
    say(result.message, !result.ok)
    await reloadPlans()
    selectedPlan.value = plans.value.find((p) => p.id === plan.id) ?? selectedPlan.value
    await loadRounds(result.round?.round_id)
  } catch (error) {
    say(error instanceof Error ? error.message : '新建轮次失败', true)
  }
}

async function triggerRoundRequest() {
  if (!selectedPlan.value) {
    say('请先选择一个计划', true)
    return
  }
  try {
    const result = await requestRound(Number(selectedPlan.value.id))
    say(result.message, !result.ok)
    await reloadPlans()
    await loadRounds()
  } catch (error) {
    say(error instanceof Error ? error.message : '队列请求失败', true)
  }
}

async function completeCurrentRound() {
  if (!selectedPlan.value || !activeRoundId.value) return
  try {
    const result = await completeRound(Number(selectedPlan.value.id), activeRoundId.value)
    say(result.message, !result.ok)
    await reloadPlans()
    await loadRounds()
  } catch (error) {
    say(error instanceof Error ? error.message : '完成回写失败', true)
  }
}

async function verifyAll() {
  if (!selectedPlan.value || !activeRoundId.value) return
  try {
    const result = await verifyDeliveries(Number(selectedPlan.value.id), activeRoundId.value)
    const summary = result.summary ?? {}
    say(
      `${result.message}：收录 ${summary['收录'] ?? 0} / 暂挂 ${summary['暂挂'] ?? 0} / ` +
      `需要恢复 ${summary['需要恢复'] ?? 0}`,
      !result.ok,
    )
    await openRoundView(activeRoundId.value)
  } catch (error) {
    say(error instanceof Error ? error.message : '交付校验失败', true)
  }
}

async function openSignoff(pageNo: string) {
  if (!selectedPlan.value || !activeRoundId.value) return
  try {
    const result = await signoffDetail(Number(selectedPlan.value.id), activeRoundId.value, pageNo)
    if (!result.ok) throw new Error(result.message)
    signoff.value = result
  } catch (error) {
    say(error instanceof Error ? error.message : '签收详情读取失败', true)
  }
}

async function refreshSignoff() {
  if (!signoff.value) return
  await openSignoff(signoff.value.page.page_no)
}

async function recordOneReceipt() {
  if (!signoff.value || !selectedPlan.value) return
  try {
    const result = await recordReceipt(
      Number(selectedPlan.value.id),
      signoff.value.round_id,
      signoff.value.page.page_no,
    )
    say(result.message, !result.ok)
    await openSignoff(signoff.value.page.page_no)
    await openRoundView(activeRoundId.value)
  } catch (error) {
    say(error instanceof Error ? error.message : '回执登记失败', true)
  }
}

async function signPageNow() {
  if (!signoff.value || !selectedPlan.value) return
  try {
    const result = await signPage(
      Number(selectedPlan.value.id),
      signoff.value.round_id,
      signoff.value.page.page_no,
    )
    say(result.message, !result.ok)
    await reloadPlans()
    await openRoundView(activeRoundId.value)
    await openSignoff(signoff.value.page.page_no)
  } catch (error) {
    say(error instanceof Error ? error.message : '签收失败', true)
  }
}

function runPageAction(action: string, pageNo: string) {
  if (action === '继续计划') {
    if (selectedPlan.value) void resumeSelected(selectedPlan.value)
    return
  }
  if (action === '登记回执') {
    void quickReceipt(pageNo)
    return
  }
  if (action === '查看完成回执') {
    void lookupCompletion(activeRoundId.value)
    return
  }
  void openSignoff(pageNo)
}

async function quickReceipt(pageNo: string) {
  if (!selectedPlan.value || !activeRoundId.value) return
  try {
    const result = await recordReceipt(Number(selectedPlan.value.id), activeRoundId.value, pageNo)
    say(result.message, !result.ok)
    await openRoundView(activeRoundId.value)
  } catch (error) {
    say(error instanceof Error ? error.message : '回执登记失败', true)
  }
}

async function lookupCompletion(roundId?: string) {
  if (!selectedPlan.value) return
  try {
    const result = await getCompletionReceipt(Number(selectedPlan.value.id), roundId)
    say(result.message, !result.ok)
  } catch (error) {
    say(error instanceof Error ? error.message : '完成回执回查失败', true)
  }
}

defineExpose({ lookupCompletion, openRoundView, signPageNow })

onMounted(async () => {
  await reloadPlans(false)
  if (plans.value.length) await selectPlan(plans.value[0])
})
</script>
