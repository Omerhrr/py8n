<!-- v138: thin ECharts wrapper used by the App builder's chart component -
     one <canvas>/<svg>-backed instance per chart, resized on container
     changes and re-rendered whenever `option` changes (deep). Client-only:
     ECharts needs a real DOM, so callers should wrap usage in <ClientOnly>. -->
<template>
  <div ref="el" :style="{ width: '100%', height: (height || 260) + 'px' }" />
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'

const props = defineProps<{ option: any; height?: number }>()

const el = ref<HTMLDivElement | null>(null)
let chart: any = null
let ro: ResizeObserver | null = null

async function render() {
  if (!chart || !props.option) return
  chart.setOption(props.option, true)
}

onMounted(async () => {
  if (!el.value) return
  const echarts = await import('echarts')
  chart = echarts.init(el.value, null, { renderer: 'svg' })
  await render()
  ro = new ResizeObserver(() => chart?.resize())
  ro.observe(el.value)
})

onBeforeUnmount(() => {
  ro?.disconnect()
  chart?.dispose()
  chart = null
})

watch(
  () => props.option,
  () => nextTick(render),
  { deep: true }
)
</script>
