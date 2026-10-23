import { reactive } from 'vue'
import { api } from '../api'

export const cart = reactive({
  customerId: null,
  items: [],

  add(product, quantity = 1) {
    this.items.push({ product_id: product.id, quantity, price_cents: product.price_cents })
  },

  async placeOrder() {
    const order = await api.post('/orders', { customer_id: this.customerId, items: this.items })
    const invoice = await api.post(`/orders/${order.id}/checkout`)
    this.items = []
    return invoice
  },
})
