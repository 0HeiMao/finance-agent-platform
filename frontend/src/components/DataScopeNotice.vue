<script setup>
// 数据范围提示条：受限账号（部门不为空）在每页顶部说明「看到的不是全公司口径」。
// 后端各个接口都会回传 scope（见 backend/scope.py 的 scope_payload），这里只负责显示，
// 没有 scope（老接口或不受限账号）时什么都不渲染。
const props = defineProps({
  scope: { type: Object, default: null }
})
</script>

<template>
  <p v-if="props.scope && props.scope.unrestricted === false" class="data-scope">
    <strong>数据范围：{{ props.scope.label }}</strong>
    — {{ props.scope.note || '当前账号只统计本部门明细，不是全公司口径。' }}
  </p>
</template>
