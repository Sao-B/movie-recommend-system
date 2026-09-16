<template>
  <!-- 前后台共用同一根 NavBar，保证 header 风格完全一致 -->
  <NavBar />
  <router-view />
</template>

<script setup>
import NavBar from '@/components/NavBar.vue'
</script>

<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--app-bg-page); }
</style>
