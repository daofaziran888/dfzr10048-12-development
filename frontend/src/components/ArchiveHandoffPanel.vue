<template>
  <section class="handoff-panel" data-testid="archive-handoff-panel">
    <header class="handoff-head">
      <div>
        <h3>按架位逐片交接读取</h3>
        <p>字节、校验、交接授权和回执均绑定原架位；其它架位的同媒体片不能代填。</p>
      </div>
      <div class="handoff-actions">
        <button class="btn" data-testid="handoff-reload" type="button" @click="reload">刷新核对</button>
        <RouterLink class="btn" data-testid="go-redelivery" to="/fail_redeliver">前往重投页试领</RouterLink>
        <button class="btn" data-testid="handoff-seed-late-a" type="button" @click="seed(true)">载入：甲完整原片已迟到</button>
        <button class="btn" data-testid="handoff-seed-b-pending" type="button" @click="seed(false)">载入：乙读取在途</button>
      </div>
    </header>

    <div v-if="message" class="handoff-message" :class="{ error: isError }" data-testid="handoff-message">
      {{ message }}
    </div>

    <div class="handoff-grid">
      <article class="mini-card" data-testid="ownership-card">
        <span>原任务归属（交接不变更）</span>
        <strong>{{ ownership.original_applicant || '原申请人-采集组' }}</strong>
        <small>{{ ownership.task_id || 'BCMP-HANDOFF-001' }}</small>
      </article>
      <article class="mini-card filled" data-testid="filled-receipt-card">
        <span>第 2 片已填缺回执</span>
        <strong data-testid="filled-receipt-id">{{ filledReceipt?.receipt_id || '暂无' }}</strong>
        <small>同一缺口结算次数：{{ shard2SettlementCount }}</small>
      </article>
      <article class="mini-card readonly" data-testid="readonly-receipt-card">
        <span>第 2 片只读留档回执</span>
        <strong data-testid="readonly-receipt-ids">{{ readonlyReceiptIds }}</strong>
        <small>不代填、不二次结算</small>
      </article>
    </div>

    <div class="handoff-actions wrap">
      <button class="btn" data-testid="handoff-transfer-b" type="button" :disabled="busy" @click="transferToB">
        值班员将第 2 片交给乙
      </button>
      <button class="btn primary" data-testid="handoff-direct-restore" type="button" :disabled="busy" @click="directRestore">
        从原架位直接恢复到补采任务
      </button>
      <button class="btn primary" data-testid="handoff-b-complete" type="button" :disabled="busy" @click="submitB(false)">
        乙完成读取后重送回执
      </button>
      <button class="btn" data-testid="handoff-b-replay" type="button" :disabled="busy" @click="submitB(true)">
        重送乙同一回执（只结算一次）
      </button>
      <button class="btn" data-testid="handoff-a-late-after-b" type="button" :disabled="busy" @click="submitAAfterB">
        让甲的完整回执迟到
      </button>
      <button class="btn warn" data-testid="handoff-half-demo" type="button" :disabled="busy" @click="runHalfDemo">
        用只有半份字节的申请试交接
      </button>
    </div>

    <div v-if="halfResult" class="half-result" data-testid="half-gap-result" :data-retained="halfResult.gap_retained">
      半份申请已提交：完整 {{ halfResult.full_bytes }} 字节，实际可读 {{ halfResult.half_bytes }} 字节；
      采集缺口<span v-if="halfResult.gap_retained" class="retained">仍保留</span>。
    </div>

    <h4>逐片交接回执（架位页核对）</h4>
    <div class="table-scroll">
      <table class="handoff-table">
        <thead>
          <tr>
            <th>回执编号</th>
            <th>分片</th>
            <th>恢复人</th>
            <th>原架位</th>
            <th>字节</th>
            <th>原校验</th>
            <th>处置</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="receipt in receipts"
            :key="String(receipt.receipt_id)"
            :data-testid="`receipt-row-${receipt.receipt_id}`"
            :data-disposition="String(receipt.disposition)"
          >
            <td>{{ receipt.receipt_id }}</td>
            <td>第 {{ receipt.shard_seq ?? '?' }} 片</td>
            <td>{{ receipt.recoverer || '—' }}</td>
            <td>{{ receipt.shelf_code || receipt.shelf_id || '—' }}</td>
            <td>{{ receipt.byte_count ?? 0 }}</td>
            <td class="mono">{{ receipt.checksum_verified ? receipt.checksum : '未通过' }}</td>
            <td>
              <span class="badge" :data-settled="Boolean(receipt.settled)" :data-readonly="Boolean(receipt.readonly)">
                {{ receipt.disposition }}
              </span>
              <em v-if="receipt.duplicate">重复投递</em>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { handoffApi, type HandoffRecord } from '@/api/handoff'

const TASK_ID = 'BCMP-HANDOFF-001'
const SHELF2 = 'ARCS-S2-ORIG'

const view = ref<HandoffRecord>({})
const seedInfo = ref<HandoffRecord | null>(null)
const message = ref('')
const isError = ref(false)
const busy = ref(false)
const halfResult = ref<HandoffRecord | null>(null)

const receipts = computed<HandoffRecord[]>(() => (view.value.receipts as HandoffRecord[]) || [])
const shelves = computed<HandoffRecord[]>(() => (view.value.shelves as HandoffRecord[]) || [])
const ownership = computed<HandoffRecord>(() => ({ task_id: TASK_ID, original_applicant: '原申请人-采集组' }))
const shard2Receipts = computed(() => receipts.value.filter((r) => String(r.shard_seq) === '2'))
const filledReceipt = computed(() => shard2Receipts.value.find((r) => r.settled))
const readonlyReceipts = computed(() => shard2Receipts.value.filter((r) => r.readonly || r.archived_readonly))
const readonlyReceiptIds = computed(() => readonlyReceipts.value.map((r) => String(r.receipt_id)).join('、') || '暂无')
const shard2SettlementCount = computed(() => (filledReceipt.value ? 1 : 0))

function shelfContent(shelfId: string): string {
  const shelf = shelves.value.find((item) => item.shelf_id === shelfId)
  return String(shelf?.content_b64 || seedInfo.value?.full2_content_b64 || '')
}

function flash(text: string, error = false) {
  message.value = text
  isError.value = error
}

async function reload() {
  const data = await handoffApi.shelfView(TASK_ID)
  view.value = data
}

async function seed(lateA: boolean) {
  busy.value = true
  try {
    const result = await handoffApi.seed(lateA)
    seedInfo.value = (result.seed as HandoffRecord) || null
    halfResult.value = null
    await reload()
    flash(lateA ? '已载入：甲的完整原片通过原校验后迟到，第 2 片可试领。' : '已载入：乙只读到一半且读取在途；此时试领应能查到乙。')
  } catch (error) {
    flash(error instanceof Error ? error.message : '场景载入失败', true)
  } finally {
    busy.value = false
  }
}

async function transferToB() {
  busy.value = true
  try {
    await handoffApi.transfer({
      task_id: TASK_ID,
      shard_seq: 2,
      from_recoverer: '甲',
      to_recoverer: '乙',
      disconnected: true,
    })
    await reload()
    flash('已按原架位第 2 片向乙发放逐片读取权限。')
  } catch (error) {
    flash(error instanceof Error ? error.message : '交接失败', true)
  } finally {
    busy.value = false
  }
}

async function directRestore() {
  busy.value = true
  try {
    const result = await handoffApi.restore({ task_id: TASK_ID, shard_seq: 2, operator: '值班员' })
    await reload()
    flash(String(result.message || (result.restored ? '已直接恢复到补采任务' : '原片未完成恢复')))
  } catch (error) {
    flash(error instanceof Error ? error.message : '直接恢复失败', true)
  } finally {
    busy.value = false
  }
}

async function submitB(replay: boolean) {
  busy.value = true
  try {
    const contentB64 = shelfContent(SHELF2)
    const result = await handoffApi.receipt({
      receipt_id: 'RCPT-B-UI-001',
      task_id: TASK_ID,
      shard_seq: 2,
      handoff_id: 'AHND-B-S2',
      recoverer: '乙',
      shelf_id: SHELF2,
      content_b64: contentB64,
      complete: true,
    })
    await reload()
    flash(replay ? `同一回执重送：${String(result.message || '不会重复结算')}` : `乙的回执结论：${String(result.disposition)}`)
  } catch (error) {
    flash(error instanceof Error ? error.message : '回执提交失败', true)
  } finally {
    busy.value = false
  }
}

async function submitAAfterB() {
  busy.value = true
  try {
    const result = await handoffApi.receipt({
      receipt_id: 'RCPT-A-LATE-UI-001',
      task_id: TASK_ID,
      shard_seq: 2,
      handoff_id: 'AHND-A-S2',
      recoverer: '甲',
      shelf_id: SHELF2,
      content_b64: shelfContent(SHELF2),
      complete: true,
    })
    await reload()
    flash(`甲的迟到回执结论：${String(result.disposition)}`)
  } catch (error) {
    flash(error instanceof Error ? error.message : '甲回执提交失败', true)
  } finally {
    busy.value = false
  }
}

async function runHalfDemo() {
  busy.value = true
  try {
    const result = await handoffApi.halfDemo()
    halfResult.value = result
    flash(result.gap_retained ? '半份字节未通过完整原校验，采集缺口已保留。' : '半份申请处理异常', !result.gap_retained)
  } catch (error) {
    flash(error instanceof Error ? error.message : '半份交接演示失败', true)
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void reload().catch((error: unknown) => flash(error instanceof Error ? error.message : '交接数据读取失败', true))
})
</script>

<style scoped>
.handoff-panel { margin: 0 0 20px; padding: 18px; border: 1px solid #d9e2ef; border-radius: 14px; background: linear-gradient(180deg, #f8fbff 0%, #fff 42%); }
.handoff-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.handoff-head h3 { margin: 0 0 6px; font-size: 18px; }
.handoff-head p { margin: 0; color: #5d6b82; font-size: 13px; }
.handoff-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.handoff-actions.wrap { margin: 14px 0; }
.btn { display: inline-flex; align-items: center; text-decoration: none; border: 1px solid #c8d4e6; background: #fff; color: #243b63; border-radius: 8px; padding: 8px 12px; cursor: pointer; font-size: 13px; }
.btn.primary { background: #2457d6; border-color: #2457d6; color: #fff; }
.btn.warn { border-color: #e2b84b; background: #fff8df; color: #765400; }
.btn:disabled { opacity: .55; cursor: wait; }
.handoff-message { margin: 12px 0; padding: 10px 12px; border-radius: 8px; background: #eef6ff; color: #1d4f91; font-size: 13px; }
.handoff-message.error { background: #fff0f0; color: #a32626; }
.handoff-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin: 14px 0; }
.mini-card { padding: 12px; border: 1px solid #dce5f2; border-radius: 10px; background: #fff; display: flex; flex-direction: column; gap: 5px; }
.mini-card span { color: #66758c; font-size: 12px; }
.mini-card strong { font-size: 15px; word-break: break-all; }
.mini-card small { color: #7a879b; }
.mini-card.filled { border-color: #9bd6b1; background: #f1fbf4; }
.mini-card.readonly { border-color: #c9cdd6; background: #f7f8fa; }
.half-result { margin: 10px 0; padding: 10px 12px; border-radius: 8px; background: #fff8df; color: #6b4d00; }
.retained { font-weight: 700; color: #a15c00; margin: 0 3px; }
.table-scroll { overflow: auto; border: 1px solid #e1e7f0; border-radius: 10px; }
.handoff-table { width: 100%; border-collapse: collapse; font-size: 13px; background: #fff; }
.handoff-table th, .handoff-table td { padding: 9px 10px; border-bottom: 1px solid #edf1f7; text-align: left; white-space: nowrap; }
.handoff-table th { background: #f3f6fb; color: #4c5b72; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.badge { display: inline-block; padding: 3px 8px; border-radius: 999px; background: #eef2f8; }
.badge[data-settled='true'] { background: #ddf7e6; color: #176b35; }
.badge[data-readonly='true'] { background: #eceff3; color: #5d6572; }
em { margin-left: 6px; color: #b06a00; font-style: normal; font-size: 12px; }
h4 { margin: 16px 0 8px; }
</style>
