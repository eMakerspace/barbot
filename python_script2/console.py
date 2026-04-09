"""Interactive CLI console for Barbot."""

from config import BarbotConfig, AttributesConfig, SPIRIT_SLOTS, MIXER_SLOTS, ALL_SLOTS
from store import StoreConfig
from woo_client import WooClient
from hardware import HardwareInterface
from inventory import InventoryManager
from orders import OrderProcessor


class Console:
    """Interactive REPL for controlling the barbot."""

    def __init__(
        self,
        config: BarbotConfig,
        store: StoreConfig,
        attributes: AttributesConfig,
        woo: WooClient,
        hardware: HardwareInterface,
        inventory: InventoryManager,
        orders: OrderProcessor,
    ):
        self.config = config
        self.store = store
        self.attributes = attributes
        self.woo = woo
        self.hw = hardware
        self.inventory = inventory
        self.orders = orders

    def run(self):
        print("\n" + "=" * 50)
        print("  BARBOT – Interactive Console")
        print("=" * 50)

        # Normalize any display names in slot_mapping to slugs
        self._normalize_slot_mapping()

        # Run homing sequence on startup
        self.hw.homing()

        # Fetch latest store data before entering menu
        self._cmd_fetch()

        # Main menu loop
        self._main_menu()

    def _normalize_slot_mapping(self):
        """Convert any display names in slot_mapping to slugs using attributes config."""
        name_to_slug = {}
        for terms in self.attributes.term_slugs.values():
            for name, slug in terms.items():
                name_to_slug[name] = slug

        if not name_to_slug:
            return

        changed = False
        for slot, ing in list(self.config.slot_mapping.items()):
            if ing in name_to_slug:
                self.config.slot_mapping[slot] = name_to_slug[ing]
                changed = True

        if changed:
            self.config.save()

    def _main_menu(self):
        """Main menu: choose between Setup or Run."""
        while True:
            print("\n" + "=" * 50)
            print("  Main Menu")
            print("=" * 50)
            print("  1. setup   – Configure slots, recipes, and store")
            print("  2. run     – Start order polling loop")
            print("  3. quit    – Exit")
            print()

            try:
                choice = input("barbot> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if choice == "1":
                self._setup_menu()
            elif choice == "2":
                self._run_polling_loop()
            elif choice == "3":
                print("Bye!")
                break
            else:
                print("  Invalid choice. Please select 1, 2, or 3.")

    def _setup_menu(self):
        """Setup menu: manage configuration."""
        while True:
            print("\n" + "=" * 50)
            print("  Setup Menu")
            print("=" * 50)
            print("  1. fetch      – Fetch products from WooCommerce")
            print("  2. status     – Show slot mapping and store info")
            print("  3. slots      – Edit slot mapping")
            print("  4. recipes    – Show preset recipes")
            print("  5. viscosity  – Manage bottle viscosity")
            print("  6. back       – Return to main menu")
            print()

            try:
                choice = input("barbot> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                return

            if choice == "1":
                self._cmd_fetch()
            elif choice == "2":
                self._cmd_status()
            elif choice == "3":
                self._cmd_slots()
            elif choice == "4":
                self._cmd_recipes()
            elif choice == "5":
                self._cmd_viscosity()
            elif choice == "6":
                break
            else:
                print("  Invalid choice. Please select 1-6.")

    def _run_polling_loop(self):
        """Start the order polling loop."""
        try:
            self.orders.poll_loop(self.config.poll_interval)
        except KeyboardInterrupt:
            print("\n[POLL] Stopped.")
        print("  Returning to main menu...")


    # -- Command implementations for setup menu ----

    def _cmd_fetch(self):
        """Fetch products and attributes from WooCommerce."""
        self._fetch_attributes()
        self.store.fetch(self.woo, self.attributes.term_slugs)

    def _fetch_attributes(self):
        """Fetch all attributes and their terms with bottle properties."""
        print("  [FETCH] Fetching attributes and bottle properties...")

        try:
            attributes = self.woo.fetch_all("products/attributes")
        except Exception as e:
            print(f"  [ERROR] Failed to fetch attributes: {e}")
            return

        attribute_ids = {}
        attribute_slugs = {}
        term_slugs = {}
        bottle_properties = {}

        for attr in attributes:
            attr_id = attr.get("id")
            attr_name = attr.get("name")
            attr_slug = attr.get("slug")

            if not attr_id or not attr_name or not attr_slug:
                continue

            attribute_ids[attr_slug] = attr_id
            attribute_slugs[attr_name] = attr_slug

            # Fetch terms for this attribute
            try:
                terms = self.woo.fetch_all(f"products/attributes/{attr_id}/terms")
            except Exception as e:
                print(f"  [WARN] Failed to fetch terms for {attr_name}: {e}")
                continue

            term_slugs[attr_slug] = {}
            bottle_properties[attr_slug] = {}

            for term in terms:
                term_id = term.get("id")
                term_name = term.get("name")
                term_slug = term.get("slug")

                if not term_id or not term_name or not term_slug:
                    continue

                term_slugs[attr_slug][term_name] = term_slug

                bp = term.get('bottle_properties', {})
                bottle_properties[attr_slug][term_slug] = {
                    "id": term_id,
                    "name": term_name,
                    "bottle_size": float(bp.get('bottle_size', 70)),
                    "viscosity":   float(bp.get('viscosity',   1)),
                }

        # Save attributes config
        self.attributes.update_attributes(attribute_ids, attribute_slugs, term_slugs, bottle_properties)
        self.attributes.save()
        print("  ✓ Attributes and bottle properties saved.")

    def _cmd_status(self):
        spirits_map, mixers_map = self._slug_to_name_maps()

        def display(slug: str) -> str:
            return spirits_map.get(slug) or mixers_map.get(slug) or slug

        print("\nSpirit Slots:")
        for slot in SPIRIT_SLOTS:
            ing = self.config.slot_ingredient(slot)
            flag = " [EMPTY]" if slot in self.config.empty_slots else ""
            print(f"  {slot}: {display(ing) if ing else '-'}{flag}")
        print("\nMixer Slots:")
        for slot in MIXER_SLOTS:
            ing = self.config.slot_ingredient(slot)
            flag = " [EMPTY]" if slot in self.config.empty_slots else ""
            print(f"  {slot}: {display(ing) if ing else '-'}{flag}")

        print(f"\nStore Config: last fetched {self.store.last_fetched or 'never'}")
        print(f"  Available Spirits: {[display(s) for s in self.store.available_spirits]}")
        print(f"  Available Mixers:  {[display(m) for m in self.store.available_mixers]}")
        print(f"  Products cached:   {len(self.store.products)}")

    def _cmd_recipes(self):
        attribute_slugs = self.attributes.attribute_slugs
        recipes = self.store.get_preset_recipes(attribute_slugs)
        if not recipes:
            print("\n  No preset recipes found. Run 'fetch' first.")
            return
        spirits_map, mixers_map = self._slug_to_name_maps()

        def display(slug: str) -> str:
            return spirits_map.get(slug) or mixers_map.get(slug) or slug

        print("\nPreset Recipes (from store_config):")
        for sku, recipe in recipes.items():
            parts = ", ".join(f"{display(i['name'])} {i['ml']:.0f}ml" for i in recipe["ingredients"])
            print(f"  {sku}: {parts}")

        diy = self.store.get_diy_volumes(attribute_slugs)
        print(f"\nDIY Volumes (from store_config): Spirit={diy['Spirit']:.0f}ml, Mixer={diy['Mixer']:.0f}ml")

    def _slug_to_name_maps(self) -> tuple[dict, dict]:
        """Build {slug: display_name} maps for spirits and mixers from attributes config."""
        spirits_map = {
            slug: name
            for name, slug in self.attributes.term_slugs.get("pa_spirits", {}).items()
        }
        mixers_map = {
            slug: name
            for name, slug in self.attributes.term_slugs.get("pa_mixers", {}).items()
        }
        return spirits_map, mixers_map

    def _cmd_slots(self):
        """Manage slot mappings with add/remove options."""
        available_spirits = sorted(self.store.available_spirits)
        available_mixers = sorted(self.store.available_mixers)
        spirits_map, mixers_map = self._slug_to_name_maps()

        def display(slug: str) -> str:
            return spirits_map.get(slug) or mixers_map.get(slug) or slug

        while True:
            print("\n" + "-" * 50)
            print("Current Slot Mappings:")
            print("-" * 50)
            print("Spirit slots (Slot_1..Slot_8):")
            for slot in SPIRIT_SLOTS:
                ing = self.config.slot_ingredient(slot)
                print(f"  {slot}: {display(ing) if ing else '-'}")
            print("\nMixer slots (Slot_A..Slot_D):")
            for slot in MIXER_SLOTS:
                ing = self.config.slot_ingredient(slot)
                print(f"  {slot}: {display(ing) if ing else '-'}")

            if available_spirits:
                print(f"\nAvailable spirits: {[display(s) for s in available_spirits]}")
                print(f"Available mixers:  {[display(m) for m in available_mixers]}")
            else:
                print("\n  [WARN] No store data loaded. Run 'fetch' first.")

            print("\nOptions:")
            print("  1. add     – Add/update a slot mapping")
            print("  2. remove  – Remove a slot mapping")
            print("  3. back    – Return to setup menu")
            print()

            try:
                choice = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                return

            if choice == "1":
                self._add_slot_mapping(available_spirits, available_mixers, spirits_map, mixers_map)
            elif choice == "2":
                self._remove_slot_mapping(spirits_map, mixers_map)
            elif choice == "3":
                break
            else:
                print("  Invalid choice. Please select 1-3.")

    def _add_slot_mapping(self, available_spirits, available_mixers, spirits_map, mixers_map):
        """Add or update a slot mapping. Stores slugs, displays names."""
        # Select slot
        print("\n[SELECT SLOT]")
        all_slots = SPIRIT_SLOTS + MIXER_SLOTS

        def display(slug: str) -> str:
            return spirits_map.get(slug) or mixers_map.get(slug) or slug

        for i, slot in enumerate(all_slots, 1):
            ing = self.config.slot_ingredient(slot)
            current = display(ing) if ing else "-"
            print(f"  {i}. {slot:10} (current: {current})")
        print(f"  {len(all_slots) + 1}. cancel")

        try:
            slot_choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not slot_choice.isdigit() or int(slot_choice) > len(all_slots) + 1:
            print("  Invalid choice.")
            return

        slot_idx = int(slot_choice) - 1
        if slot_idx == len(all_slots):
            return

        selected_slot = all_slots[slot_idx]

        # Select ingredient — show display names, index into slug list
        print(f"\n[SELECT INGREDIENT FOR {selected_slot}]")
        if BarbotConfig.is_spirit_slot(selected_slot):
            slugs = available_spirits
            name_map = spirits_map
        else:
            slugs = available_mixers
            name_map = mixers_map

        for i, slug in enumerate(slugs, 1):
            print(f"  {i}. {name_map.get(slug, slug)}")
        print(f"  {len(slugs) + 1}. cancel")

        try:
            ing_choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not ing_choice.isdigit() or int(ing_choice) > len(slugs) + 1:
            print("  Invalid choice.")
            return

        ing_idx = int(ing_choice) - 1
        if ing_idx == len(slugs):
            return

        selected_slug = slugs[ing_idx]

        self.config.set_slot(selected_slot, selected_slug)
        self.config.save()
        print(f"  ✓ Set {selected_slot} -> {name_map.get(selected_slug, selected_slug)}")
        self._sync_inventory()

    def _remove_slot_mapping(self, spirits_map: dict, mixers_map: dict):
        """Remove a slot mapping."""
        print("\n[SELECT SLOT TO CLEAR]")
        filled_slots = [(s, self.config.slot_ingredient(s)) for s in ALL_SLOTS if self.config.slot_ingredient(s)]

        if not filled_slots:
            print("  No slots to clear.")
            return

        def display(slug: str) -> str:
            return spirits_map.get(slug) or mixers_map.get(slug) or slug

        for i, (slot, ing) in enumerate(filled_slots, 1):
            print(f"  {i}. {slot:10} -> {display(ing)}")
        print(f"  {len(filled_slots) + 1}. cancel")

        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not choice.isdigit() or int(choice) < 1 or int(choice) > len(filled_slots) + 1:
            print("  Invalid choice.")
            return

        choice_idx = int(choice) - 1
        if choice_idx == len(filled_slots):  # cancel
            return

        slot_to_clear = filled_slots[choice_idx][0]
        self.config.clear_slot(slot_to_clear)
        self.config.save()
        print(f"  ✓ Cleared {slot_to_clear}")
        self._sync_inventory()

    def _sync_inventory(self):
        """Sync current slot configuration to WooCommerce."""
        print("\n  [SYNC] Updating WooCommerce stock status...")
        self.inventory.push_all()

    def _cmd_viscosity(self):
        """Manage bottle viscosity from store config."""
        print("\n[VISCOSITY] Loading from store configuration...")

        # Extract viscosity data from store config
        spirits_viscosity = self._extract_bottle_viscosity("pa_spirits")
        mixers_viscosity = self._extract_bottle_viscosity("pa_mixers")

        if not spirits_viscosity and not mixers_viscosity:
            print("  [ERROR] No spirits or mixers found in store config.")
            return

        # Main viscosity menu
        while True:
            print("\n" + "-" * 50)
            print("Viscosity Management:")
            print("-" * 50)
            menu_items = []
            if spirits_viscosity:
                menu_items.append("spirits")
                print(f"  {len(menu_items)}. spirits  – Set viscosity for spirits")
            if mixers_viscosity:
                menu_items.append("mixers")
                print(f"  {len(menu_items)}. mixers   – Set viscosity for mixers")
            print(f"  {len(menu_items) + 1}. back     – Return to setup menu")
            print()

            try:
                choice = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                return

            if not choice.isdigit() or int(choice) < 1 or int(choice) > len(menu_items) + 1:
                print("  Invalid choice.")
                continue

            choice_idx = int(choice) - 1
            if choice_idx == len(menu_items):
                break
            elif menu_items[choice_idx] == "spirits":
                self._manage_attribute_viscosity("Spirits", "pa_spirits", spirits_viscosity)
                break
            elif menu_items[choice_idx] == "mixers":
                self._manage_attribute_viscosity("Mixers", "pa_mixers", mixers_viscosity)
                break

    def _extract_bottle_viscosity(self, attr_slug: str) -> dict[str, dict]:
        """Extract bottle viscosity data from attributes config."""
        viscosity_map = {}

        if attr_slug not in self.attributes.bottle_properties:
            return viscosity_map

        bottles = self.attributes.bottle_properties[attr_slug]
        for term_slug, bottle_data in bottles.items():
            bottle_name = bottle_data.get("name", term_slug)
            viscosity_map[bottle_name] = {
                "viscosity": bottle_data.get("viscosity", 1),
                "term_id": bottle_data.get("id"),
                "term_slug": term_slug,
                "attr_slug": attr_slug,
            }

        return viscosity_map

    def _manage_attribute_viscosity(self, attr_name: str, attr_slug: str, viscosity_map: dict[str, dict]):
        """Manage viscosity for a specific attribute (Spirits or Mixers)."""
        print(f"\n[{attr_name.upper()}] Select Bottle:")
        print("-" * 50)

        items = list(viscosity_map.items())
        item_map = {}
        for i, (bottle_name, data) in enumerate(items, 1):
            viscosity = data["viscosity"]
            print(f"  {i}. {bottle_name:30} – Viscosity: {viscosity}")
            item_map[str(i)] = (bottle_name, data)

        print(f"  {len(items) + 1}. cancel")

        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == str(len(items) + 1):
            return

        if choice not in item_map:
            print("  Invalid selection.")
            return

        bottle_name, data = item_map[choice]

        # Select viscosity value from presets
        viscosity_presets = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        current_viscosity = data["viscosity"]

        print(f"\n[{bottle_name}] Select Viscosity:")
        for i, v in enumerate(viscosity_presets, 1):
            marker = " ← current" if v == current_viscosity else ""
            print(f"  {i}. {v}{marker}")
        print(f"  {len(viscosity_presets) + 1}. cancel")

        try:
            visc_choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if visc_choice == str(len(viscosity_presets) + 1):
            return

        if not visc_choice.isdigit() or int(visc_choice) > len(viscosity_presets):
            print("  Invalid selection.")
            return

        new_viscosity = viscosity_presets[int(visc_choice) - 1]

        # Update in attributes config
        term_slug = data["term_slug"]
        if attr_slug in self.attributes.bottle_properties:
            if term_slug in self.attributes.bottle_properties[attr_slug]:
                self.attributes.bottle_properties[attr_slug][term_slug]["viscosity"] = new_viscosity

        self.attributes.save()

        # Get IDs from attributes config
        attr_id = self.attributes.attribute_ids.get(attr_slug)
        term_id = data["term_id"]

        # Update WooCommerce
        if attr_id and term_id:
            if self.woo.update_term_viscosity(attr_id, term_id, new_viscosity):
                print(f"  ✓ Updated '{bottle_name}' viscosity to {new_viscosity}")
        else:
            print(f"  ⚠ Updated local config for '{bottle_name}' viscosity to {new_viscosity}")
            print(f"    (Missing WooCommerce IDs for API update)")
