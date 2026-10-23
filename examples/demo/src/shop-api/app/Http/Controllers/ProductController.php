<?php

namespace App\Http\Controllers;

use App\Models\Product;

class ProductController extends Controller
{
    public function index()
    {
        return Product::query()->where('stock', '>', 0)->get();
    }

    public function show(Product $product)
    {
        return $product;
    }
}
