<template>
  <section class="page" data-module="resume">
    <header class="page-head">
      <div>
        <h2>断点续采恢复管理</h2>
        <p class="page-desc">维护断点续采的位置记录、进度追踪与恢复策略，断点字段与恢复状态原子保存，恢复回执与归并计数按同一回执核对。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记断点记录</button>
        <button class="btn" type="button" @click="exportRows">导出断点续采恢复清单</button>
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
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>已确认水位</th>
          <th>回执版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.confirmed_watermark ?? '—' }}</td>
          <td>{{ row.receipt_version ?? 0 }}</td>
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
            <button class="link" type="button" @click="revalidate(row)">重新校验</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无断点续采恢复数据，可先登记断点记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条断点续采恢复记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="linked-panel">
      <header class="panel-head">
        <h3>归并下游联动数据</h3>
        <div class="page-actions">
          <button class="btn" type="button" @click="loadLinked">刷新联动数据</button>
        </div>
      </header>

      <h4>恢复回执账本（完成回执与恢复回执共用递增版本号）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>版本</th><th>断点编号</th><th>回执类型</th><th>批次号</th>
            <th>水位</th><th>数量</th><th>说明</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="receipt in receipts" :key="String(receipt.id)">
            <td>#{{ receipt.receipt_no }}</td>
            <td>{{ receipt.断点编号 }}</td>
            <td>{{ receiptKindText(receipt.kind) }}</td>
            <td>{{ receipt.batch_id }}</td>
            <td>{{ receipt.watermark }}</td>
            <td>{{ receipt.amount }}</td>
            <td>{{ receipt.note }}</td>
            <td>{{ receipt.created_at }}</td>
          </tr>
          <tr v-if="!receipts.length">
            <td colspan="8" class="empty-state">暂无恢复回执</td>
          </tr>
        </tbody>
      </table>

      <h4>续采恢复结果（写入归并下游，按恢复回执核对）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>回执</th><th>断点编号</th><th>批次号</th><th>恢复量</th>
            <th>水位</th><th>归并增量</th><th>归并状态</th><th>递延说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="result in results" :key="String(result.id)">
            <td>#{{ result.receipt_no }}</td>
            <td>{{ result.断点编号 }}</td>
            <td>{{ result.batch_id }}</td>
            <td>{{ result.recovered_count }}</td>
            <td>{{ result.watermark }}</td>
            <td>{{ result.merge_delta }}</td>
            <td>{{ result.merge_status }}</td>
            <td>{{ result.defer_reason ?? '—' }}</td>
          </tr>
          <tr v-if="!results.length">
            <td colspan="8" class="empty-state">暂无续采恢复结果</td>
          </tr>
        </tbody>
      </table>

      <h4>修复断点字段丢失核对清单</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>断点编号</th><th>字段</th><th>问题</th><th>详情</th>
            <th>批次号</th><th>状态</th><th>触发环节</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in checklist" :key="String(item.id)">
            <td>{{ item.断点编号 }}</td>
            <td>{{ item.field }}</td>
            <td>{{ item.issue }}</td>
            <td>{{ item.detail ?? '—' }}</td>
            <td>{{ item.batch_id ?? '—' }}</td>
            <td>{{ item.status === 'open' ? '待修复' : '已修复' }}</td>
            <td>{{ item.trigger }}</td>
            <td>{{ item.created_at }}</td>
          </tr>
          <tr v-if="!checklist.length">
            <td colspan="8" class="empty-state">核对清单为空，断点字段与恢复状态一致</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type LinkedRow = Record<string, string | number | null>

const ENDPOINT = '/api/resume'
const columns = ["断点编号", "采集计划", "断点位置", "已采集量", "剩余量", "恢复策略", "恢复耗时", "记录状态"]
const actions = ["启动恢复", "放弃恢复", "归档记录"]
const statuses = ["已记录", "恢复中", "已恢复", "已放弃"]
const stats = [{"label": "断点记录总数", "value": 0}, {"label": "恢复中断点", "value": 0}, {"label": "已恢复断点", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const receipts = ref<LinkedRow[]>([])
const results = ref<LinkedRow[]>([])
const checklist = ref<LinkedRow[]>([])

function receiptKindText(kind: string | number | null): string {
  if (kind === 'completion') return '完成回执'
  if (kind === 'recovery') return '恢复回执'
  if (kind === 'failure') return '失败回执'
  return String(kind ?? '—')
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '断点记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('断点续采恢复动作未生效，请稍后重试')
    }
    const payload = await response.json()
    // 后端按约定返回 200 + ok=false 表达业务拦截（如状态回退、归档后变更），
    // 必须显式提示，否则列表会残留旧值让用户误以为动作生效
    if (!payload.ok) {
      throw new Error(payload.message || '断点续采恢复动作未生效')
    }
    await reload()
    await loadLinked()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '断点续采恢复操作失败'
  }
}

async function revalidate(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/revalidate`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '恢复状态重新校验失败')
    }
    errorMessage.value = payload.message
    await reload()
    await loadLinked()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复状态重新校验失败'
  }
}

async function loadLinked() {
  try {
    const [receiptResp, resultResp, checklistResp] = await Promise.all([
      request(`${ENDPOINT}/receipts`),
      request(`${ENDPOINT}/results`),
      request(`${ENDPOINT}/checklist`),
    ])
    const receiptPayload = await receiptResp.json()
    const resultPayload = await resultResp.json()
    const checklistPayload = await checklistResp.json()
    receipts.value = receiptPayload.items ?? []
    results.value = resultPayload.items ?? []
    checklist.value = checklistPayload.items ?? []
  } catch {
    errorMessage.value = '归并下游联动数据读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('断点记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '断点续采恢复列表读取失败'
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
