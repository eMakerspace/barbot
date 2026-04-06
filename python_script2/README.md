# Barbot - Automated Bartender WooCommerce Integration


Anleitung schreiben!!

Neuer Drink im onlineshop:

1. Bild generieren
2. Anderen Drink duplizieren
3. Bild, Text, Titel ersetzen
4. Inhalt ersetzen (attribute)
5. Preis setzen
6. Auf "Out of Stock" setzen
7. Veröffentlichen

Neues Attribut (Spirituose, Mixer) hinzufügen:
1. Unter Attribut -> Mixer -> add term (screenshot!)
2. Properties der Flasche (Bottle size, Viscosity)
3. Unter DIY Drink: Neue Attribute hinzufügen
4. Unter DIY Drink: Neue Variationen hinzufügen
5. Unter DIY Drink: Preise setzen für alle Variationen

Wenn es mehr als 200 Variationen gibt: Snippet anpassen.
<!-- A Python control script for a physical drink-mixing machine integrated with a WooCommerce storefront. The machine polls for paid orders, dispenses drinks via hardware pumps, and keeps the online store in sync with physical inventory.

## Architecture

```
WooCommerce Store                    Barbot Machine
┌──────────────────┐                ┌──────────────────────────────┐
│  Customer UI     │   REST API     │  main.py (entry point)       │
│  Payment         │◄──────────────►│  ├── config.py               │
│  Product catalog │   wc/v3        │  ├── woo_client.py           │
│  Order mgmt      │                │  ├── store.py                │
│  Heartbeat EP    │◄── ping ──────►│  ├── hardware.py             │
└──────────────────┘                │  ├── inventory.py            │
                                    │  ├── orders.py               │
                                    │  └── console.py              │
                                    └──────────────────────────────┘
```

The machine is the **source of truth** for physical inventory. WooCommerce handles UI, payments, and order creation. The machine polls for paid orders, executes them, and tells WooCommerce what's in/out of stock.

## Quick Start

### 1. Setup

```bash
cd python_script2
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

Copy and edit the `.env` file:

```
WOOCOMMERCE_URL=https://your-store.example.com
WOOCOMMERCE_KEY=ck_your_consumer_key
WOOCOMMERCE_SECRET=cs_your_consumer_secret
HEARTBEAT_TOKEN=your_secret_token
```

### 3. Run

```bash
source venv/bin/activate
python main.py
```

### 4. First-time initialization

```
barbot> fetch        # Download product catalog from WooCommerce
barbot> slots        # Assign ingredients to physical pump slots
barbot> push         # Sync store stock to match mounted bottles
barbot> poll         # Start processing orders
```

## CLI Commands

| Command | Description |
|---|---|
| `fetch` | Pull all products and attributes from WooCommerce into `store_config.json` |
| `push` | Full stock sync: products are `instock` only if all their ingredients are mounted |
| `status` | Show current slot mapping, empty slots, and store info |
| `slots` | Interactive editor for slot-to-ingredient mapping |
| `recipes` | Show all preset recipes and DIY volumes derived from the store |
| `poll` | Start the order polling loop (Ctrl-C to stop) |
| `empty <slot>` | Mark a slot as empty (sets affected products to `outofstock`) |
| `refill <slot>` | Mark a slot as refilled (sets affected products back to `instock`) |
| `simulate <sku>` | Simulate dispensing a preset drink locally (no API calls) |
| `help` | Show available commands |
| `quit` | Exit |

## Slot System

The machine has **12 physical slots**:

| Slots | Type | Count | Examples |
|---|---|---|---|
| `Slot_1` .. `Slot_8` | Spirit pumps | up to 8 | vodka, gin, rum, tequila |
| `Slot_A` .. `Slot_D` | Mixer pumps | up to 4 | cola, tonic-water, orangensaft |

Spirit slots only accept spirit ingredient slugs, mixer slots only accept mixer slugs. Available ingredients are determined by what exists in the WooCommerce store (fetched via `fetch`).

### Editing slots

```
barbot> slots
Spirit slots (Slot_1..Slot_8):
  Slot_1: vodka
  Slot_2: gin
  ...

Available spirits: ['gin', 'jaegermeister', 'rum', 'tequila', 'trojka-green', 'trojka-red', 'vodka']
Available mixers:  ['bitter-lemon', 'citro', 'cola', 'ice-tea', 'orangensaft', 'tonic-water']

Enter 'Slot_X=ingredient-slug' or 'Slot_X=' to clear (blank line to finish):
  > Slot_5=trojka-red
  Set Slot_5 -> trojka-red
  >
  Config saved.
```

## Configuration Files

### `.env` - WooCommerce Credentials

```
WOOCOMMERCE_URL=https://barbot.emakerspace.ch
WOOCOMMERCE_KEY=ck_...
WOOCOMMERCE_SECRET=cs_...
HEARTBEAT_TOKEN=...
```

This file is **gitignored** and must not be committed.

### `config.json` - Hardware Configuration

Local-only settings that define the physical machine state. Not pulled from WooCommerce.

```json
{
    "poll_interval_seconds": 5,
    "slot_mapping": {
        "Slot_1": "vodka",
        "Slot_2": "gin",
        "Slot_A": "cola",
        "Slot_B": "orangensaft"
    }
}
```

- `poll_interval_seconds` - How often (in seconds) to check for new orders
- `slot_mapping` - Maps physical slot IDs to ingredient slugs

### `store_config.json` - Cached WooCommerce Catalog

Populated by the `fetch` command. Contains:

| Field | Description |
|---|---|
| `last_fetched` | ISO timestamp of last sync |
| `attribute_slugs` | WooCommerce attribute display name to slug mapping (e.g. `"Spirits"` -> `"pa_spirits"`) |
| `term_slugs` | Per-attribute term name to slug mapping (e.g. `"Vodka"` -> `"vodka"`) |
| `available_spirits` | All spirit slugs available in the store |
| `available_mixers` | All mixer slugs available in the store |
| `products` | Cached product data with id, name, slug, sku, type, status, stock_status, and attributes |

All attribute names and values are stored as **slugs** for consistent matching. The store uses WooCommerce product attributes: `Spirits`, `Mixers`, `Spirits Amount` (in cl), and `Mixers Amount` (in cl).

## Module Reference

### `config.py` - Configuration & Slot Definitions

**Constants:** `SPIRIT_SLOTS`, `MIXER_SLOTS`, `ALL_SLOTS`

**`BarbotConfig`** - Manages `config.json` and runtime slot state:
- `slot_mapping` - Slot-to-ingredient assignments
- `empty_slots` - Runtime set of slots marked as empty
- `ingredient_to_slot` - Auto-rebuilt reverse lookup
- `mounted_ingredients()` - Returns set of ingredients in non-empty slots
- `set_slot()` / `clear_slot()` - Modify mappings and rebuild lookup
- `save()` - Persist to `config.json`

### `woo_client.py` - WooCommerce API Wrapper

**`WooClient`** - Wraps the WooCommerce REST API (v3):
- `fetch_all(endpoint, params)` - Paginated GET (100 items/page, auto-pagination)
- `batch_update_products(updates)` - Batch stock status update for simple products
- `batch_update_variations(parent_id, updates)` - Batch update for variable product variations
- `update_order_status(order_id, status, retries=3)` - Update order with retry logic (2s between attempts)
- `send_heartbeat()` - POST to `/wp-json/barmachine/v1/ping`

### `store.py` - Store Catalog & Recipe Derivation

**`StoreConfig`** - Caches WooCommerce product data and derives recipes:
- `fetch(woo)` - Pull all products and attribute terms, rebuild cache
- `get_preset_recipes()` - Build recipe dict from simple products. Amounts are converted from cl (store) to ml (machine)
- `get_diy_volumes()` - Extract default Spirit/Mixer pour volumes from the first variable product
- `attr_slug(display_name)` - Resolve attribute display name to WooCommerce slug
- `parse_cl_to_ml(val)` - Parse values like `"4"`, `"4 cl"`, or `["4 cl"]` into ml

### `hardware.py` - Hardware Abstraction

**`HardwareInterface`** - Dummy implementation for development. Subclass for real GPIO:
- `dispense(slot, ml)` - Activate pump (currently prints + 0.3s sleep)
- `display_order_id(short_id)` - Show 2-digit ID on display
- `signal_done()` - LED/buzzer for drink ready
- `check_slot_sensor(slot)` - Check if slot has liquid

### `inventory.py` - Inventory Sync

**`InventoryManager`** - Keeps WooCommerce stock in sync with physical state:
- `push_all()` - Full sync: every product/variation is set `instock` or `outofstock` based on which ingredients are mounted
- `sync_ingredient(ingredient, in_stock)` - Toggle stock for all products using one specific ingredient
- `mark_empty(slot)` / `mark_refill(slot)` - Convenience methods that update slot state + sync

### `orders.py` - Order Processing

**`OrderProcessor`** - Parses and executes WooCommerce orders:
- `parse_line_item(item)` - Resolves a line item to `[{slot, ingredient, ml}]`. Handles both preset (simple) and DIY (variable) products
- `process_order(order)` - Full pipeline: display ID, dispense all items, signal done, mark order completed
- `poll_loop(interval)` - Blocking loop: heartbeat + fetch processing orders + dispense + complete

### `console.py` - Interactive CLI

**`Console`** - REPL that dispatches to the other modules. All commands listed in the [CLI Commands](#cli-commands) section above.

## Workflows

### Order Processing Flow

```
poll_loop() ─► send_heartbeat()
             ─► fetch orders (status=processing)
             ─► for each order:
                  ├── display_order_id(order_id % 100)
                  ├── for each line_item:
                  │     ├── preset? → lookup recipe in store_config
                  │     └── DIY?    → extract spirit/mixer from meta_data
                  │     └── dispense(slot, ml) for each ingredient
                  ├── signal_done()
                  └── update order status → "completed"
```

### Inventory Sync Flow (`push`)

```
push_all() ─► get mounted ingredients from slot_mapping (minus empty slots)
           ─► fetch all products from WooCommerce
           ─► for each simple product:
           │     all ingredients mounted? → instock : outofstock
           ─► for each variable product variation:
           │     spirit + mixer both mounted? → instock : outofstock
           ─► batch update WooCommerce
```

Products set to `outofstock` become invisible in the store, so customers only see drinks the machine can currently make.

### Empty/Refill Flow

```
empty Slot_1 ─► add Slot_1 to empty_slots
             ─► sync_ingredient("vodka", in_stock=False)
             ─► all products using "vodka" → outofstock

refill Slot_1 ─► remove Slot_1 from empty_slots
              ─► sync_ingredient("vodka", in_stock=True)
              ─► all products using "vodka" → instock
```

## Extending

### Real Hardware

Subclass `HardwareInterface` in `hardware.py`:

```python
class RealHardware(HardwareInterface):
    def dispense(self, slot: str, ml: float):
        # Activate GPIO pin for slot, run pump for calculated duration
        ...

    def signal_done(self):
        # Trigger buzzer/LED
        ...
```

Then in `main.py`, replace `HardwareInterface(config)` with `RealHardware(config)`.

### Adding CLI Commands

Add a method to `Console` and register it in the `handler` dict inside `run()`.

### Custom Order Logic

Override `OrderProcessor.parse_line_item()` or `process_order()` for special handling. -->
