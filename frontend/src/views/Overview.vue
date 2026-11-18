<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>

    <section v-if="bindingView" class="binding-view">
      <h3>可执行环境绑定 · 结果视图（处理项虎）</h3>
      <p class="page-desc">凭据与环境在同一绑定中同时确定，运行批次的凭据来源和环境版本可回查。</p>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">绑定总数</span>
          <strong class="stat-value">{{ bindingView.total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前可执行绑定</span>
          <strong class="stat-value">{{ bindingView.current }}</strong>
        </article>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>绑定状态</th><th>数量</th></tr>
        </thead>
        <tbody>
          <tr v-for="(count, status) in bindingView.by_status" :key="status">
            <td>{{ status }}</td>
            <td>{{ count }}</td>
          </tr>
        </tbody>
      </table>
      <table class="data-table">
        <thead>
          <tr><th>25号处理终态</th><th>数量</th></tr>
        </thead>
        <tbody>
          <tr v-for="(count, terminal) in bindingView.by_terminal" :key="terminal">
            <td>{{ terminal }}</td>
            <td>{{ count }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

type BindingResultView = {
  total: number
  current: number
  by_status: Record<string, number>
  by_terminal: Record<string, number>
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])
const bindingView = ref<BindingResultView | null>(null)

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "流程定义", "created": 0, "pending": 0, "abnormal": 0}, {"name": "步骤编排", "created": 0, "pending": 0, "abnormal": 0}, {"name": "元素定位", "created": 0, "pending": 0, "abnormal": 0}, {"name": "触发配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "执行记录", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据提取", "created": 0, "pending": 0, "abnormal": 0}, {"name": "凭据管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "环境配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "反封锁", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果校验", "created": 0, "pending": 0, "abnormal": 0}, {"name": "批次追踪", "created": 0, "pending": 0, "abnormal": 0}, {"name": "页面建模", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据补全", "created": 0, "pending": 0, "abnormal": 0}, {"name": "代理路由", "created": 0, "pending": 0, "abnormal": 0}, {"name": "频控策略", "created": 0, "pending": 0, "abnormal": 0}, {"name": "输出格式", "created": 0, "pending": 0, "abnormal": 0}, {"name": "异常日志", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调度计划", "created": 0, "pending": 0, "abnormal": 0}, {"name": "运行概览", "created": 0, "pending": 0, "abnormal": 0}, {"name": "任务队列", "created": 0, "pending": 0, "abnormal": 0}, {"name": "环境绑定", "created": 0, "pending": 0, "abnormal": 0}]
  }
  try {
    bindingView.value = await fetchJson<BindingResultView>('/api/env-bindings/result-view')
  } catch {
    bindingView.value = null
  }
})
</script>
