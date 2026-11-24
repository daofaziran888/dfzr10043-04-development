<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="meta.key === 'field_mapping'" class="trial-panel" data-testid="batch-trial-panel">
      <h3 class="trial-title">批量试算</h3>
      <p class="trial-desc">整批采集记录按映射当前版本过同一套转换口径，整数与空值判定逐条落明细。</p>
      <div class="trial-controls">
        <label class="trial-field">
          <span>映射规则</span>
          <select v-model="trialMappingId" data-testid="trial-mapping">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row['映射编号'] }} · {{ row['转换函数'] }} · 空值{{ row['空值策略'] }} · {{ row['映射版本'] }}
            </option>
          </select>
        </label>
        <label class="trial-field trial-samples-field">
          <span>样例值（每行一条，空行按空值处理）</span>
          <textarea v-model="trialSamples" rows="4" data-testid="trial-samples" placeholder="007&#10;&#10;7x"></textarea>
        </label>
        <button class="btn primary" type="button" data-testid="run-batch-trial" :disabled="trialing" @click="runBatchTrial">
          {{ trialing ? '试算中…' : '批量试算' }}
        </button>
      </div>
      <template v-if="trialResult">
        <p class="trial-summary" data-testid="trial-summary">
          共 {{ trialResult.total }} 条：转换成功 {{ trialResult.converted }}、空值跳过 {{ trialResult.skipped }}、
          转换失败 {{ trialResult.failed }}（映射版本 {{ trialResult['映射版本'] }}）
        </p>
        <table class="data-table trial-table">
          <thead>
            <tr><th>来源</th><th>输入</th><th>判定</th><th>输出</th><th>映射版本</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in trialResult.items" :key="item.source_id" :data-status="item.status">
              <td>{{ item.source_id }}</td>
              <td>{{ item.input === null || item.input === '' ? '(空)' : item.input }}</td>
              <td>{{ trialStatusLabel(item.status) }}</td>
              <td>{{ item.output ?? '—' }}</td>
              <td>{{ item['映射版本'] }}</td>
              <td>{{ item.reason || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  batchTrial,
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { BatchTrialResult } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const trialMappingId = ref<number | null>(null)
const trialSamples = ref('')
const trialing = ref(false)
const trialResult = ref<BatchTrialResult | null>(null)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (meta.key === 'field_mapping' && trialMappingId.value === null && rows.value.length) {
      trialMappingId.value = Number(rows.value[0].id)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

function trialStatusLabel(status: string): string {
  if (status === 'converted') return '转换成功'
  if (status === 'skipped') return '空值跳过'
  return '转换失败'
}

async function runBatchTrial() {
  if (trialMappingId.value === null) {
    errorMessage.value = '请先选择映射规则'
    return
  }
  trialing.value = true
  errorMessage.value = ''
  try {
    const lines = trialSamples.value.split('\n')
    while (lines.length && lines[lines.length - 1].trim() === '') lines.pop()
    const records = lines.map((line, index) => ({
      source_id: `S${index + 1}`,
      value: line.trim() === '' ? null : line.trim(),
    }))
    trialResult.value = await batchTrial(trialMappingId.value, records)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量试算失败'
  } finally {
    trialing.value = false
  }
}

onMounted(reload)
</script>
