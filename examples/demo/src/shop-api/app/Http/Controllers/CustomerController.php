<?php

namespace App\Http\Controllers;

use App\Models\Customer;
use Illuminate\Http\Request;

class CustomerController extends Controller
{
    public function index()
    {
        return Customer::query()->orderBy('name')->paginate();
    }

    public function show(Customer $customer)
    {
        return $customer->load('orders');
    }

    public function store(Request $request)
    {
        return Customer::create($request->validate([
            'name' => 'required|string',
            'email' => 'required|email',
        ]));
    }
}
