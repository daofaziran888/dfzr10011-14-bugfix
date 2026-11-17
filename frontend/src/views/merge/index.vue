<template>
  <section class="page" data-module="merge">
    <header class="page-head">
      <div>
        <h2>结果聚合归并管理</h2>
        <p class="page-desc">聚合任务读取续采恢复结果并按恢复回执更新归并计数，修复断点字段丢失核对清单与断点水位按同一回执核对。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记聚合任务</button>
        <button class="btn" type="button" @click="exportRows">导出结果聚合归并清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">读取</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>已核回执</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ appliedReceipts(row).length }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="applyPending(row)">拉取恢复结果</button>
            <button class="link" type="button" @click="reconcile(row)">回执核对</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无结果聚合归并数据，可先登记聚合任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条结果聚合归并记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="linked-panel">
      <header class="panel-head">
        <h3>断点续采联动数据</h3>
        <div class="page-actions">
          <button class="btn" type="button" @click="loadLinked">刷新联动数据</button>
        </div>
      </header>

      <h4>续采恢复结果（按恢复回执幂等归并）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>回执</th><th>断点编号</th><th>采集计划</th><th>批次号</th>
            <th>恢复量</th><th>水位</th><th>归并增量</th><th>归并状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="result in resumeResults" :key="String(result.id)">
            <td>#{{ result.receipt_no }}</td>
            <td>{{ result.断点编号 }}</td>
            <td>{{ result.采集计划 }}</td>
            <td>{{ result.batch_id }}</td>
            <td>{{ result.recovered_count }}</td>
            <td>{{ result.watermark }}</td>
            <td>{{ result.merge_delta }}</td>
            <td>{{ result.merge_status }}</td>
            <td>
              <button
                v-if="result.merge_status !== 'applied'"
                class="link"
                type="button"
                @click="applyResult(result.id)"
              >
                按回执归并
              </button>
              <span v-else>已归并</span>
            </td>
          </tr>
          <tr v-if="!resumeResults.length">
            <td colspan="9" class="empty-state">暂无续采恢复结果</td>
          </tr>
        </tbody>
      </table>

      <h4>修复断点字段丢失核对清单（其他业务入口联动）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>断点编号</th><th>采集计划</th><th>字段</th><th>问题</th>
            <th>详情</th><th>状态</th><th>触发环节</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in checklist" :key="String(item.id)">
            <td>{{ item.断点编号 }}</td>
            <td>{{ item.采集计划 }}</td>
            <td>{{ item.field }}</td>
            <td>{{ item.issue }}</td>
            <td>{{ item.detail ?? '—' }}</td>
            <td>{{ item.status === 'open' ? '待修复' : '已修复' }}</td>
            <td>{{ item.trigger }}</td>
            <td>{{ item.created_at }}</td>
          </tr>
          <tr v-if="!checklist.length">
            <td colspan="8" class="empty-state">核对清单为空，断点字段与归并计数一致</td>
          </tr>
        </tbody>
      </table>

      <h4 v-if="reconcileReport">回执核对结果</h4>
      <table v-if="reconcileReport" class="data-table">
        <tbody>
          <tr><th>断点编号</th><td>{{ reconcileReport.断点编号 }}</td></tr>
          <tr><th>已确认水位</th><td>{{ reconcileReport.confirmed_watermark }}</td></tr>
          <tr><th>最新回执版本</th><td>#{{ reconcileReport.last_receipt_no }}</td></tr>
          <tr><th>已归并回执增量合计</th><td>{{ reconcileReport.applied_receipt_delta }}</td></tr>
          <tr><th>聚合任务</th><td>{{ reconcileReport.merge_task ?? '—' }}</td></tr>
          <tr><th>归并计数</th><td>{{ reconcileReport.merge_count }}</td></tr>
          <tr><th>核对结论</th><td>{{ reconcileReport.receipt_consistent ? '一致' : '不一致' }}</td></tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | number[]>
type LinkedRow = Record<string, string | number | null>

const ENDPOINT = '/api/merge'
const columns = ["任务编号", "采集计划", "分片总数", "已归并数", "归并策略", "输出路径", "数据总量", "任务状态"]
const actions = ["启动归并", "完成归并", "重试归并", "归档任务"]
const statuses = ["待归并", "归并中", "已完成", "归并失败"]
const stats = [{"label": "聚合任务总数", "value": 0}, {"label": "归并中任务", "value": 0}, {"label": "已完成任务", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const resumeResults = ref<LinkedRow[]>([])
const checklist = ref<LinkedRow[]>([])
const reconcileReport = ref<Record<string, unknown> | null>(null)

function appliedReceipts(row: Row): number[] {
  return Array.isArray(row.applied_receipts) ? (row.applied_receipts as number[]) : []
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '聚合任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('结果聚合归并动作未生效，请稍后重试')
    }
    const payload = await response.json()
    // 状态机非法流转（如归档后变更、状态回退）返回 ok=false，必须提示，
    // 不能静默保留旧状态让用户误以为动作生效
    if (!payload.ok) {
      throw new Error(payload.message || '结果聚合归并动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果聚合归并操作失败'
  }
}

async function applyResult(resultId: string | number | null) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/resume-results/${resultId}/apply`, { method: 'POST' })
    const payload = await response.json()
    errorMessage.value = payload.message
    await reload()
    await loadLinked()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复结果归并失败'
  }
}

async function applyPending(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/apply-pending`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '恢复结果拉取失败')
    }
    errorMessage.value = payload.message
    await reload()
    await loadLinked()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复结果拉取失败'
  }
}

async function reconcile(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/reconcile`)
    reconcileReport.value = await response.json()
  } catch {
    errorMessage.value = '回执核对失败'
  }
}

async function loadLinked() {
  try {
    const [resultResp, checklistResp] = await Promise.all([
      request(`${ENDPOINT}/resume-results`),
      request(`${ENDPOINT}/repair-checklist`),
    ])
    const resultPayload = await resultResp.json()
    const checklistPayload = await checklistResp.json()
    resumeResults.value = resultPayload.items ?? []
    checklist.value = checklistPayload.items ?? []
  } catch {
    errorMessage.value = '断点续采联动数据读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('聚合任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果聚合归并列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadLinked()
})
</script>

<style scoped>
.linked-panel {
  margin-top: 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.linked-panel h3 {
  margin: 0;
}

.linked-panel h4 {
  margin: 8px 0 0;
  font-size: 14px;
}
</style>
