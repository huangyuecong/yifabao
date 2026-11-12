<script setup lang="ts">
import { contentRoutePageKey } from '@/router/videoEditorRouteSession'

defineOptions({ name: 'ContentLayout' })
</script>

<template>
  <div class="content-layout">
    <main id="content-main" class="content-layout-main">
      <RouterView v-slot="{ Component, route }">
        <KeepAlive include="ContentWorkbench">
          <component :is="Component" :key="contentRoutePageKey(route)" />
        </KeepAlive>
      </RouterView>
    </main>
  </div>
</template>

<style scoped>
.content-layout {
  display: flex;
  height: 100dvh;
  flex-direction: column;
  color: var(--xi-semantic-color-text-primary);
  background: var(--xi-semantic-color-background-page);
}
.content-layout-main {
  min-height: 0;
  flex: 1;
  overflow: auto;
}
</style>
