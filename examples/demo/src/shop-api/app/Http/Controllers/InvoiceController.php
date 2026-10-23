<?php

namespace App\Http\Controllers;

use App\Models\Invoice;

class InvoiceController extends Controller
{
    public function index()
    {
        return Invoice::query()->with('order.customer')->latest('issued_at')->paginate();
    }

    public function show(Invoice $invoice)
    {
        return $invoice->load('order.items');
    }
}
