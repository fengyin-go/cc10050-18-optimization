<template>
  <section class="page" data-module="price_crawler">
    <header class="page-head">
      <div>
        <h2>价格爬取任务</h2>
        <p class="page-desc">按关键词与深度抓取商品价格页；每笔页面交付先用采集状态数据校验资格，再把收录 / 暂挂 / 需要恢复回传调度。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="reload-tasks" @click="reload">刷新任务</button>
      </div>
    </header>

    <p v-if="notice" class="notice" :class="{ error: noticeIsError }" data-testid="crawler-notice">{{ notice }}</p>

    <table class="data-table">
      <thead>
        <tr>
          <th>任务名称</th>
          <th>目标站点</th>
          <th>商品关键词</th>
          <th>抓取深度</th>
          <th>采集间隔</th>
          <th>当前状态</th>
          <th>当前轮次</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="task in tasks"
          :key="task.id"
          :data-testid="`task-${task.id}`"
          :data-status="String(task.status)"
        >
          <td>{{ task['任务名称'] }}</td>
          <td>{{ task['目标站点'] }}</td>
          <td>{{ task['商品关键词'] }}</td>
          <td>{{ task['抓取深度'] }}</td>
          <td>{{ task['采集间隔'] }}</td>
          <td>{{ task.status }}</td>
          <td>{{ task.round_id || '—' }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`verify-all-${task.id}`"
              @click="verifyTask(task)"
            >
              校验全部页面交付
            </button>
          </td>
        </tr>
        <tr v-if="!tasks.length">
          <td colspan="8" class="empty-state">暂无价格爬取任务</td>
        </tr>
      </tbody>
    </table>

    <section v-if="activeTask" class="round-panel" data-testid="delivery-panel">
      <header class="round-head">
        <div>
          <h3>{{ activeTask['任务名称'] }} · 轮次 {{ result?.round_id || activeTask.round_id || '—' }}</h3>
          <p class="page-desc">
            采集状态数据来源：{{ result?.source || 'crawl_state' }} · 校验时间 {{ result?.checked_at || '—' }}
          </p>
        </div>
        <span v-if="result">
          收录 {{ result.summary['收录'] ?? 0 }} / 暂挂 {{ result.summary['暂挂'] ?? 0 }} /
          需要恢复 {{ result.summary['需要恢复'] ?? 0 }}
        </span>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>页面</th>
            <th>轮次</th>
            <th>回执</th>
            <th>入库样本</th>
            <th>交付结果</th>
            <th>下游交接</th>
            <th>依据</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in result?.results || []"
            :key="item.page_no"
            :data-testid="`delivery-${item.page_no}`"
            :data-outcome="item.outcome"
          >
            <td>{{ item.page_no }}</td>
            <td>{{ item.round_id }}</td>
            <td>{{ item.receipts }}</td>
            <td>{{ item.stored_sample_id ?? '—' }}</td>
            <td><strong class="elig" :class="`elig-${item.outcome}`">{{ item.outcome }}</strong></td>
            <td>{{ item.handoff }}</td>
            <td>
              <ul class="reason-list">
                <li v-for="reason in item.reasons" :key="reason">{{ reason }}</li>
              </ul>
            </td>
          </tr>
        </tbody>
      </table>

      <h4 class="sub-title">回到原申请：按实际接收顺序可做的操作</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>顺序</th>
            <th>页面</th>
            <th>排序依据</th>
            <th>交付资格</th>
            <th>可做操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="op in result?.page_operations || []"
            :key="`op-${op.page_no}`"
            :data-testid="`operation-${op.page_no}`"
          >
            <td>{{ op.sequence }}</td>
            <td>{{ op.page_no }}</td>
            <td>{{ op.order_basis }}</td>
            <td>{{ op.eligibility }}</td>
            <td>{{ op.available_actions.join('、') || '无' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import { deliverTaskPages, type DeliveryOutcome, type PageOperation } from '@/api/crawl'
import type { EntryRow } from '@/data/types'

type TaskEntry = EntryRow & { round_id?: string }

type DeliveryResult = {
  ok: boolean
  message: string
  round_id?: string
  source?: string
  checked_at?: string
  summary: Record<string, number>
  results: DeliveryOutcome[]
  page_operations: PageOperation[]
}

const tasks = ref<TaskEntry[]>([])
const activeTask = ref<TaskEntry | null>(null)
const result = ref<DeliveryResult | null>(null)
const notice = ref('')
const noticeIsError = ref(false)

function say(message: string, isError = false) {
  notice.value = message
  noticeIsError.value = isError
}

async function reload() {
  try {
    const payload = await listEntries('price_crawler')
    tasks.value = payload.items as TaskEntry[]
  } catch (error) {
    say(error instanceof Error ? error.message : '任务读取失败', true)
  }
}

async function verifyTask(task: TaskEntry) {
  activeTask.value = task
  result.value = null
  try {
    const response = await deliverTaskPages(Number(task.id), task.round_id || undefined)
    if (!response.ok) {
      say(response.message, true)
      return
    }
    result.value = {
      ok: response.ok,
      message: response.message,
      round_id: response.round_id,
      source: response.source,
      checked_at: response.checked_at,
      summary: response.summary ?? {},
      results: response.results ?? [],
      page_operations: response.page_operations ?? [],
    }
    say(
      `交付校验完成：收录 ${result.value.summary['收录'] ?? 0} / ` +
      `暂挂 ${result.value.summary['暂挂'] ?? 0} / 需要恢复 ${result.value.summary['需要恢复'] ?? 0}`,
    )
  } catch (error) {
    say(error instanceof Error ? error.message : '交付校验失败', true)
  }
}

onMounted(reload)
</script>
