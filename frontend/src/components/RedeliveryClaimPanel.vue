<template>
  <section class="claim-panel" data-testid="redelivery-claim-panel">
    <header class="claim-head">
      <div>
        <h3>归档原片恢复后试领</h3>
        <p>可领时必须返回通过原校验的完整录音和采用依据；不可领时指明仍需哪位恢复人结算。</p>
      </div>
      <RouterLink class="btn" data-testid="go-archive-shelf" to="/archive_shelf">前往架位页核对</RouterLink>
      <button class="btn" data-testid="claim-reload" type="button" @click="reload">刷新重投视图</button>
    </header>

    <form class="claim-form" @submit.prevent="claim(customShard)">
      <label>
        补采任务
        <input v-model="taskId" data-testid="claim-task-id" />
      </label>
      <label>
        分片序号/编号
        <input v-model="customShard" data-testid="claim-shard" />
      </label>
      <button class="btn primary" data-testid="claim-submit" type="submit">试领</button>
      <button class="btn" data-testid="claim-shard-2" type="button" @click="claim('2')">试领第 2 片</button>
      <button class="btn" data-testid="claim-shard-3" type="button" @click="claim('3')">试领第 3 片</button>
    </form>

    <div v-if="error" class="claim-error" data-testid="claim-error">{{ error }}</div>

    <article
      v-if="result"
      class="claim-result"
      :data-claimable="Boolean(result.claimable)"
      data-testid="claim-result"
    >
      <div class="claim-status">
        <strong v-if="result.claimable" class="ok" data-testid="claimable">可领：完整录音已通过原校验</strong>
        <strong v-else class="blocked" data-testid="not-claimable">暂不可领</strong>
        <span>{{ result.message }}</span>
      </div>

      <div v-if="result.claimable" class="recording" data-testid="claim-recording">
        <div><b>分片：</b>第 {{ result.shard_seq ?? recording.shard_seq ?? '?' }} 片 / {{ result.关联分片 }}</div>
        <div><b>原架位：</b>{{ recording.shelf_code || basis.原架位 }}</div>
        <div><b>字节数：</b>{{ recording.byte_count }}</div>
        <div><b>原校验：</b><code>{{ recording.checksum }}</code></div>
        <div><b>采用回执：</b>{{ basis.receipt_id }}</div>
        <div><b>采用依据：</b>{{ basis.采用规则 || basis.rule }}</div>
        <a
          v-if="downloadHref"
          class="download"
          :href="downloadHref"
          :download="`shard-${result.shard_seq || 'recording'}.bin`"
          data-testid="download-recording"
        >下载完整录音字节</a>
      </div>

      <div v-else class="pending" data-testid="pending-recoverer">
        仍需恢复人
        <strong>{{ pendingName }}</strong>
        结算<span v-if="result.in_flight">（读取仍在途）</span>。
      </div>
    </article>

    <h4>重投页缺口</h4>
    <div class="gap-list">
      <article v-for="gap in gaps" :key="String(gap.shard_key)" class="gap-card" :data-claimable="Boolean(gap.claimable)">
        <div class="gap-title">第 {{ gap.shard_seq ?? gap.shard_key }} 片 · {{ gap.status }}</div>
        <div>{{ gap.claim_message }}</div>
        <small v-if="gapPendingName(gap)">待结算：{{ gapPendingName(gap) }}</small>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { handoffApi, type HandoffRecord } from '@/api/handoff'

const taskId = ref('BCMP-HANDOFF-001')
const customShard = ref('2')
const result = ref<HandoffRecord | null>(null)
const view = ref<HandoffRecord>({})
const error = ref('')

const gaps = computed<HandoffRecord[]>(() => {
  const tasks = (view.value.tasks as HandoffRecord[]) || []
  return tasks.flatMap((task) => (task.gaps as HandoffRecord[]) || [])
})

const pendingName = computed(() => {
  const pending = result.value?.pending_recoverer as HandoffRecord | undefined
  return String(pending?.name || pending?.recoverer || '值班员指定')
})

const recording = computed<HandoffRecord>(
  () => (result.value?.recording as HandoffRecord | undefined) || {},
)
const basis = computed<HandoffRecord>(
  () => (result.value?.adoption_basis as HandoffRecord | undefined) || {},
)

function gapPendingName(gap: HandoffRecord): string {
  const pending = gap.pending_recoverer as HandoffRecord | undefined
  return String(pending?.name || pending?.recoverer || '')
}

const downloadHref = computed(() => {
  const b64 = recording.value.content_b64
  return b64 ? `data:application/octet-stream;base64,${String(b64)}` : ''
})

async function claim(shard: string | number) {
  error.value = ''
  try {
    result.value = await handoffApi.claim({ task_id: taskId.value, shard_seq: shard })
    await reload()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '试领失败'
  }
}

async function reload() {
  try {
    view.value = await handoffApi.redeliveryView(taskId.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '重投视图读取失败'
  }
}

onMounted(() => {
  void reload()
})
</script>

<style scoped>
.claim-panel { margin: 0 0 20px; padding: 18px; border: 1px solid #d9e2ef; border-radius: 14px; background: linear-gradient(180deg, #f7fbff 0%, #fff 45%); }
.claim-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.claim-head h3 { margin: 0 0 6px; font-size: 18px; }
.claim-head p { margin: 0; color: #5d6b82; font-size: 13px; }
.claim-form { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; margin: 14px 0; }
.claim-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #5d6b82; }
.claim-form input { border: 1px solid #c8d4e6; border-radius: 8px; padding: 8px 10px; min-width: 180px; }
.btn { display: inline-flex; align-items: center; text-decoration: none; border: 1px solid #c8d4e6; background: #fff; color: #243b63; border-radius: 8px; padding: 8px 12px; cursor: pointer; }
.btn.primary { background: #2457d6; border-color: #2457d6; color: #fff; }
.claim-error { padding: 10px 12px; border-radius: 8px; background: #fff0f0; color: #a32626; }
.claim-result { padding: 14px; border: 1px solid #dce5f2; border-radius: 12px; background: #fff; }
.claim-result[data-claimable='true'] { border-color: #8bd4a8; background: #f4fcf7; }
.claim-result[data-claimable='false'] { border-color: #e5c66b; background: #fffdf2; }
.claim-status { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
.ok { color: #176b35; }
.blocked { color: #8a5a00; }
.recording { display: grid; gap: 6px; font-size: 13px; }
.recording code { background: #eef2f8; padding: 2px 6px; border-radius: 6px; }
.download { width: fit-content; color: #2457d6; }
.pending { font-size: 15px; color: #765400; }
.pending strong { font-size: 18px; margin: 0 4px; }
.gap-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.gap-card { padding: 12px; border: 1px solid #dce5f2; border-radius: 10px; background: #fff; font-size: 13px; display: grid; gap: 6px; }
.gap-card[data-claimable='true'] { border-color: #9bd6b1; background: #f1fbf4; }
.gap-title { font-weight: 700; }
.gap-card small { color: #8a5a00; }
h4 { margin: 16px 0 8px; }
</style>
