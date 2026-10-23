<template>
  <form @submit.prevent="create">
    <input v-model="name" placeholder="Name"> <input v-model="email" placeholder="Email">
    <button>Add customer</button>
  </form>
  <ul><li v-for="c in customers" :key="c.id">{{ c.name }} &lt;{{ c.email }}&gt;</li></ul>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const customers = ref([])
const name = ref('')
const email = ref('')

async function load() { customers.value = (await api.get('/customers')).data }
async function create() {
  await api.post('/customers', { name: name.value, email: email.value })
  await load()
}
onMounted(load)
</script>
