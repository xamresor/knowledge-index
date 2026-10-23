<template>
  <table>
    <tr v-for="o in orders" :key="o.id" @click="open(o.id)">
      <td>#{{ o.id }}</td><td>{{ o.customer.name }}</td><td>{{ o.status }}</td>
    </tr>
  </table>
  <pre v-if="current">{{ current }}</pre>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const orders = ref([])
const current = ref(null)
onMounted(async () => { orders.value = (await api.get('/orders')).data })

async function open(id) {
  current.value = await api.get(`/orders/${id}`)
}
</script>
