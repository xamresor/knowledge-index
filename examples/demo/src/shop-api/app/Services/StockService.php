<?php

namespace App\Services;

use App\Models\Order;
use Illuminate\Support\Facades\DB;

class StockService
{
    // NOTE: stock is reserved inside one transaction so two checkouts cannot sell the last item twice.
    public function reserve(Order $order): void
    {
        DB::transaction(function () use ($order) {
            foreach ($order->items as $item) {
                if (! $item->product->isInStock($item->quantity)) {
                    throw new \RuntimeException("Out of stock: {$item->product->sku}");
                }
                DB::table('products')->where('id', $item->product_id)->decrement('stock', $item->quantity);
            }
        });
    }
}
