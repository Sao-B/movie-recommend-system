<template>
  <NavBar />
  <router-view />
</template>

<script setup>
import NavBar from '@/components/NavBar.vue'
</script>

<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f7fa; }
</style>
