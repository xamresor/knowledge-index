<?php

namespace App\Services;

use App\Models\Invoice;
use App\Models\Order;

class InvoiceService
{
    // WHY: the invoice number is issued once and never reused, even if the invoice is voided.
    public function issueFor(Order $order): Invoice
    {
        $order->recalculateTotal();

        return Invoice::create([
            'order_id' => $order->id,
            'number' => $this->nextNumber(),
            'amount_cents' => $order->total_cents,
            'issued_at' => now(),
        ]);
    }

    private function nextNumber(): string
    {
        return 'INV-' . str_pad((string) (Invoice::max('id') + 1), 6, '0', STR_PAD_LEFT);
    }
}
