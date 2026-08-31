<script setup lang="ts">
// Py8n brand mark: the "8" from the name drawn as a workflow loop.
// Two tangent loops (two stages in one cycle) bound by an amber node dot
// at the waist (the automation joining them), on the product's orange-rose tile.
withDefaults(defineProps<{ size?: number }>(), { size: 32 })
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 64 64"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    role="img"
    aria-label="Py8n"
    class="shrink-0"
  >
    <title>Py8n</title>
    <defs>
      <linearGradient id="py8n-tile" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#F97316" />
        <stop offset="1" stop-color="#F43F5E" />
      </linearGradient>
    </defs>
    <!-- tile -->
    <rect width="64" height="64" rx="14" fill="url(#py8n-tile)" />
    <rect width="64" height="64" rx="14" fill="white" opacity="0.04" />
    <!-- figure 8: two tangent loops -->
    <circle cx="32" cy="20.5" r="11.5" stroke="white" stroke-width="6.5" />
    <circle cx="32" cy="43.5" r="11.5" stroke="white" stroke-width="6.5" />
    <!-- the node that binds the loop -->
    <circle cx="32" cy="32" r="4" fill="#FCD34D" />
  </svg>
</template>
