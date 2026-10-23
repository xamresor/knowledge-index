<template>
  <ul><li v-for="i in invoices" :key="i.id">{{ i.number }} — {{ i.order.customer.name }}</li></ul>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const invoices = ref([])
onMounted(async () => { invoices.value = (await api.get('/invoices')).data })
</script>
