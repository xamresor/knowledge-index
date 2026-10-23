<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Invoice extends Model
{
    protected $table = 'invoices';

    protected $fillable = ['order_id', 'number', 'amount_cents', 'issued_at'];

    public function order(): BelongsTo
    {
        return $this->belongsTo(Order::class);
    }
}
