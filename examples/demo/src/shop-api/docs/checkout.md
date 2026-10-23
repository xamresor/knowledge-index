# Checkout

A customer pays for an order in one call: `POST /api/orders/{order}/checkout`.

1. `OrderController.checkout` asks `StockService` to reserve every item. Stock is decremented inside one
   transaction, so the last item cannot be sold twice.
2. The order status becomes `paid`.
3. `InvoiceService` recalculates the order total and issues an invoice with the next free number.

See [invoices](invoices.md) for how numbers are assigned.
