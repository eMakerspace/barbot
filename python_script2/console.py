"""Interactive CLI console for Barbot."""

from config import BarbotConfig, SPIRIT_SLOTS, MIXER_SLOTS, ALL_SLOTS
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from inventory import InventoryManager
from orders import OrderProcessor


class Console:
    """Interactive REPL for controlling the barbot."""

    HELP_TEXT = """
Available commands:
  fetch           – Fetch products from WooCommerce into store_config.json
  push            – Sync store stock: only mounted ingredients are instock
  status          – Show slot mapping, empty slots, and store info
  slots           – Edit slot mapping (only available ingredients allowed)
  recipes         – Show preset recipes (from store_config)
  poll            – Start order polling loop (blocking)
  empty <slot>    – Mark a slot as empty (triggers inventory sync)
  refill <slot>   – Mark a slot as refilled (triggers restock)
  simulate <sku>  – Simulate processing a preset order locally
  help            – Show this help
  quit            – Exit
"""

    def __init__(
        self,
        config: BarbotConfig,
        store: StoreConfig,
        woo: WooClient,
        hardware: HardwareInterface,
        inventory: InventoryManager,
        orders: OrderProcessor,
    ):
        self.config = config
        self.store = store
        self.woo = woo
        self.hw = hardware
        self.inventory = inventory
        self.orders = orders

    def run(self):
        print("\n" + "=" * 50)
        print("  BARBOT – Interactive Console")
        print("=" * 50)
        print(self.HELP_TEXT)

        while True:
            try:
                raw = input("\nbarbot> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if not raw:
                continue

            parts = raw.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            handler = {
                "help":     lambda _: print(self.HELP_TEXT),
                "fetch":    lambda _: self._cmd_fetch(),
                "push":     lambda _: self.inventory.push_all(),
                "status":   lambda _: self._cmd_status(),
                "slots":    lambda _: self._cmd_slots(),
                "recipes":  lambda _: self._cmd_recipes(),
                "poll":     lambda _: self._cmd_poll(),
                "empty":    lambda a: self._cmd_with_slot(a, "empty"),
                "refill":   lambda a: self._cmd_with_slot(a, "refill"),
                "simulate": lambda a: self._cmd_simulate(a),
                "quit":     None,
                "exit":     None,
                "q":        None,
            }.get(cmd)

            if handler is None and cmd in ("quit", "exit", "q"):
                print("Bye!")
                break
            elif handler is not None:
                handler(arg)
            else:
                print(f"  Unknown command '{cmd}'. Type 'help' for options.")

    # -- Command implementations ---------------------------------------------

    def _cmd_fetch(self):
        self.store.fetch(self.woo)

    def _cmd_status(self):
        print("\nSpirit Slots:")
        for slot in SPIRIT_SLOTS:
            ing = self.config.slot_ingredient(slot) or "-"
            flag = " [EMPTY]" if slot in self.config.empty_slots else ""
            print(f"  {slot}: {ing}{flag}")
        print("\nMixer Slots:")
        for slot in MIXER_SLOTS:
            ing = self.config.slot_ingredient(slot) or "-"
            flag = " [EMPTY]" if slot in self.config.empty_slots else ""
            print(f"  {slot}: {ing}{flag}")

        print(f"\nStore Config: last fetched {self.store.last_fetched or 'never'}")
        print(f"  Available Spirits: {self.store.available_spirits}")
        print(f"  Available Mixers:  {self.store.available_mixers}")
        print(f"  Products cached:   {len(self.store.products)}")

    def _cmd_recipes(self):
        recipes = self.store.get_preset_recipes()
        if not recipes:
            print("\n  No preset recipes found. Run 'fetch' first.")
            return
        print("\nPreset Recipes (from store_config):")
        for sku, recipe in recipes.items():
            parts = ", ".join(f"{i['name']} {i['ml']:.0f}ml" for i in recipe["ingredients"])
            print(f"  {sku}: {parts}")

        diy = self.store.get_diy_volumes()
        print(f"\nDIY Volumes (from store_config): Spirit={diy['Spirit']:.0f}ml, Mixer={diy['Mixer']:.0f}ml")

    def _cmd_poll(self):
        try:
            self.orders.poll_loop(self.config.poll_interval)
        except KeyboardInterrupt:
            print("\n[POLL] Stopped.")

    def _cmd_with_slot(self, arg: str, action: str):
        if not arg:
            print(f"  Usage: {action} <slot>  (e.g. {action} Slot_1)")
            return
        if action == "empty":
            self.inventory.mark_empty(arg)
        else:
            self.inventory.mark_refill(arg)

    def _cmd_simulate(self, sku: str):
        if not sku:
            print("  Usage: simulate <sku>  (e.g. simulate CUBA-LIBRE)")
            return
        sku_upper = sku.upper()
        recipes = self.store.get_preset_recipes()
        recipe = recipes.get(sku_upper)
        if not recipe:
            print(f"  Unknown recipe '{sku}'. Available: {list(recipes.keys())}")
            return
        fake_item = {"sku": sku_upper, "name": sku_upper, "quantity": 1, "meta_data": []}
        print(f"\n  Simulating order for '{sku_upper}':")
        pours = self.orders.parse_line_item(fake_item)
        for p in pours:
            self.hw.dispense(p["slot"], p["ml"])
        self.hw.signal_done()

    def _cmd_slots(self):
        available_spirits = set(self.store.available_spirits)
        available_mixers = set(self.store.available_mixers)
        all_slugs = available_spirits | available_mixers

        print("\nSpirit slots (Slot_1..Slot_8):")
        for slot in SPIRIT_SLOTS:
            print(f"  {slot}: {self.config.slot_ingredient(slot) or '-'}")
        print("\nMixer slots (Slot_A..Slot_D):")
        for slot in MIXER_SLOTS:
            print(f"  {slot}: {self.config.slot_ingredient(slot) or '-'}")

        if available_spirits:
            print(f"\nAvailable spirits: {sorted(available_spirits)}")
            print(f"Available mixers:  {sorted(available_mixers)}")
        else:
            print("\n  [WARN] No store data loaded. Run 'fetch' first to restrict to available ingredients.")

        print("\nEnter 'Slot_X=ingredient-slug' or 'Slot_X=' to clear (blank line to finish):")
        while True:
            line = input("  > ").strip()
            if not line:
                break
            if "=" not in line:
                print("  Format: Slot_X=ingredient-slug")
                continue
            slot, _, ing = line.partition("=")
            slot, ing = slot.strip(), ing.strip()
            if not BarbotConfig.is_valid_slot(slot):
                print(f"  Invalid slot '{slot}'. Valid: {ALL_SLOTS}")
                continue
            if not ing:
                self.config.clear_slot(slot)
                print(f"  Cleared {slot}")
                continue
            if all_slugs:
                if BarbotConfig.is_spirit_slot(slot) and ing not in available_spirits:
                    print(f"  '{ing}' is not a spirit. Choose from: {sorted(available_spirits)}")
                    continue
                if BarbotConfig.is_mixer_slot(slot) and ing not in available_mixers:
                    print(f"  '{ing}' is not a mixer. Choose from: {sorted(available_mixers)}")
                    continue
            self.config.set_slot(slot, ing)
            print(f"  Set {slot} -> {ing}")

        self.config.save()
        print("  Config saved.")
