<script setup>
// Matches the server's bounded evidence_snapshot payload; excerpts are rendered as text.
defineProps({ snapshots: { type: Array, default: () => [] } })
</script>

<template>
  <section v-if="snapshots.length" class="evidence-cards" aria-label="查询证据">
    <h3>
      核对分析依据 <small>{{ snapshots.length }} 份查询快照</small>
    </h3>
    <details v-for="snapshot in snapshots" :key="snapshot.id" class="evidence-card">
      <summary>
        <strong>{{ snapshot.id }}</strong> {{ snapshot.excerpts[0]?.title || '查询依据'
        }}<span
          >{{ snapshot.scope_label
          }}<template v-if="snapshot.periods.length"> · {{ snapshot.periods.join(' / ') }}</template></span
        >
      </summary>
      <article v-for="(excerpt, index) in snapshot.excerpts" :key="index">
        <h4>{{ excerpt.title }}</h4>
        <p>{{ excerpt.text || '此条记录未提供可展示的片段。' }}</p>
      </article>
      <p class="evidence-note">{{ snapshot.note }}</p>
    </details>
  </section>
</template>
