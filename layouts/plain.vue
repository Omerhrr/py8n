<script setup lang="ts">
// Chrome-less layout for public runtime pages (v30 standalone forms) —
// no sidebar, no command palette; just the page itself.
</script>

<template>
  <div class="h-screen overflow-hidden bg-zinc-950 text-zinc-100">
    <main class="h-full overflow-y-auto">
      <slot />
    </main>
  </div>
</template>
