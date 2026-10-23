<template>
  <ul>
    <li v-for="p in products" :key="p.id">
      {{ p.title }} — {{ (p.price_cents / 100).toFixed(2) }} €
      <button @click="cart.add(p)">Add to cart</button>
    </li>
  </ul>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { cart } from '../stores/cart'

const products = ref([])
onMounted(async () => { products.value = await api.get('/products') })
</script>
