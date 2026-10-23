# Graph Report - examples/demo  (2026-10-23)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 75 nodes · 130 edges · 8 communities
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 9 edges (avg confidence: 0.86)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- shop-api · Http\Controllers\OrderController.php
- shop-api · Models
- shop-api · Http\Controllers\InvoiceController.php + Services
- shop-api · Models
- shop-api · views + Http\Controllers\CustomerController.php
- shop-web · views

## God Nodes (most connected - your core abstractions)
1. `Order` - 15 edges
2. `Invoice` - 9 edges
3. `OrderController` - 8 edges
4. `Customer` - 7 edges
5. `Product` - 7 edges
6. `InvoiceService` - 6 edges
7. `api` - 6 edges
8. `OrderItem` - 5 edges
9. `StockService` - 5 edges
10. `CustomerController` - 4 edges

## Surprising Connections (you probably didn't know these)
- `OrderController` --references--> `InvoiceService`  [EXTRACTED]
  app/Http/Controllers/OrderController.php → app/Services/InvoiceService.php
- `OrderController` --references--> `StockService`  [EXTRACTED]
  app/Http/Controllers/OrderController.php → app/Services/StockService.php

## Import Cycles
- None detected.

## Communities (8 total, 0 thin omitted)

### Community 0 - "shop-api · Http\Controllers\OrderController.php"
Cohesion: 0.29
Nodes (3): OrderController, Order, StockService

### Community 1 - "shop-api · Models"
Cohesion: 0.24
Nodes (4): OrderItem, Illuminate\Database\Eloquent\Model, Illuminate\Database\Eloquent\Relations\BelongsTo, Illuminate\Database\Eloquent\Relations\HasOne

### Community 2 - "shop-api · Http\Controllers\InvoiceController.php + Services"
Cohesion: 0.24
Nodes (4): InvoiceController, Invoice, InvoiceService, invoices

### Community 3 - "shop-api · Models"
Cohesion: 0.21
Nodes (3): ProductController, Product, Illuminate\Database\Eloquent\Relations\HasMany

### Community 4 - "shop-api · views + Http\Controllers\CustomerController.php"
Cohesion: 0.23
Nodes (8): CustomerController, Customer, Illuminate\Http\Request, create(), customers, email, load(), name

### Community 5 - "shop-web · views"
Cohesion: 0.27
Nodes (5): api, cart, current, orders, products

## Knowledge Gaps
- **7 isolated node(s):** `customers`, `name`, `email`, `invoices`, `orders` (+2 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Order` connect `shop-api · Http\Controllers\OrderController.php` to `shop-api · Models`, `shop-api · Http\Controllers\InvoiceController.php + Services`, `shop-api · Models`?**
  _High betweenness centrality (0.300) - this node is a cross-community bridge._
- **Why does `Invoice` connect `shop-api · Http\Controllers\InvoiceController.php + Services` to `shop-api · Models`?**
  _High betweenness centrality (0.142) - this node is a cross-community bridge._
- **Why does `Customer` connect `shop-api · views + Http\Controllers\CustomerController.php` to `shop-api · Models`, `shop-api · Models`?**
  _High betweenness centrality (0.135) - this node is a cross-community bridge._
- **What connects `customers`, `name`, `email` to the rest of the system?**
  _7 weakly-connected nodes found - possible documentation gaps or missing edges._