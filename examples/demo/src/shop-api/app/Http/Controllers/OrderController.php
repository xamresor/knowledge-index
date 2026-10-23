<?php

namespace App\Http\Controllers;

use App\Models\Order;
use App\Services\InvoiceService;
use App\Services\StockService;
use Illuminate\Http\Request;

class OrderController extends Controller
{
    public function __construct(
        private StockService $stock,
        private InvoiceService $invoices,
    ) {
    }

    public function index()
    {
        return Order::query()->with('customer')->latest()->paginate();
    }

    public function show(Order $order)
    {
        return $order->load(['items.product', 'invoice']);
    }

    public function store(Request $request)
    {
        $order = Order::create($request->validate(['customer_id' => 'required|exists:customers,id']));
        $order->items()->createMany($request->input('items', []));

        return $order;
    }

    public function checkout(Order $order)
    {
        $this->stock->reserve($order);
        $order->update(['status' => 'paid']);

        return $this->invoices->issueFor($order);
    }
}
