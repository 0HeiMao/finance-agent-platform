<script setup>
import { computed } from 'vue'

// 列表页共用的翻页条。刻意只做「上一页 / 下一页 + 第 x/y 页」：
// 这个项目里的列表最长也就几千行（导入批次、来源、审计），页码跳转的收益远小于
// 一个能被误用的「跳到第 N 页」输入框；而「共 N 条」必须是后端给的 total，
// 不能拿当前页条数冒充——那正是改造前三个管理页的毛病。
const props = defineProps({
  total: { type: Number, default: 0 },
  limit: { type: Number, default: 50 },
  offset: { type: Number, default: 0 },
  loading: { type: Boolean, default: false },
  unit: { type: String, default: '条' },
})

const emit = defineEmits(['change'])

const page = computed(() => Math.floor(props.offset / Math.max(props.limit, 1)) + 1)
const pages = computed(() => Math.max(1, Math.ceil(props.total / Math.max(props.limit, 1))))
const from = computed(() => (props.total ? props.offset + 1 : 0))
const to = computed(() => Math.min(props.offset + props.limit, props.total))

function turn(delta) {
  const next = props.offset + delta * props.limit
  if (next < 0 || next >= props.total) return
  emit('change', next)
}
</script>

<template>
  <div class="pager">
    <button class="btn" :disabled="page <= 1 || loading" @click="turn(-1)">上一页</button>
    <span class="muted">
      第 {{ page }} / {{ pages }} 页
      <template v-if="total">· 当前 {{ from }}–{{ to }} {{ unit }} / 共 {{ total }} {{ unit }}</template>
    </span>
    <button class="btn" :disabled="page >= pages || loading" @click="turn(1)">下一页</button>
  </div>
</template>

<style scoped>
.pager { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.pager .muted { font-size: 12px; }
</style>
