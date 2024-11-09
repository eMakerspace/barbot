from dataclasses import dataclass, field
from typing import List, Optional
import re
from woocommerce import API
import api_config


@dataclass
class Drink:
    name: str
    filler: str
    alcohol: str
    made: Optional[bool] = False

    def __repr__(self):
        return f"{self.name:<10} ┃ {self.filler:<16} ┃ {self.alcohol:<10}"

    @classmethod
    def from_json(cls, data: dict):
        name = data.get("name", "")
        filler = None
        alcohol = None

        meta_data = data.get("meta_data", [])
        if len(meta_data) > 0:
            config_value = meta_data[2].get("display_value")
            pattern = re.compile(r'Filler: (.*?) Alkohol: (.*?)$')
            match = pattern.search(config_value)
            filler, alcohol = match.groups()
        else:
            match name:
                case "Vodka-O":
                    filler = "Orangensaft"
                    alcohol = "Vodka"
                case "Rum-Cola":
                    filler = "Cola"
                    alcohol = "Rum"

        return cls(name=name, filler=filler, alcohol=alcohol)


@dataclass
class Order:
    id: int
    customer: str
    total: str
    status: str
    drinks: List[Drink] = field(default_factory=list)

    @property
    def qty(self):
        return len(self.drinks)

    def __repr__(self):
        string = "━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━\n"
        string += f"{self.id:>6} ┃ {self.customer:<29} ┃ {self.status}\n"
        string += "━━━━━━━╋━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━╋━━━━━━━━━━━━━━\n"
        string += "    ID ┃ Name       ┃ Filler           ┃ Alc\n"
        string += "───────╂────────────╂──────────────────╂──────────────\n"
        for index, drink in enumerate(self.drinks):
            string += f"{index:>6} ┃ {drink}\n"
        return string

    @classmethod
    def from_json(cls, data: dict):
        expanded_items = []
        for item in data.get("line_items", []):
            drink = Drink.from_json(item)
            quantity = item.get("quantity", 1)
            expanded_items.extend([drink] * quantity)

        return cls(
            id=data.get("id", 0),
            customer=data.get("billing", {}).get("first_name", ""),
            total=data.get("total", "0.00"),
            status=data.get("status", ""),
            drinks=expanded_items
        )


class NoMoreDrinksError(Exception):
    pass


class BarBotAPI(API):
    def __init__(self):
        super().__init__(url="https://barbot.emakerspace.ch/",
                         consumer_key=api_config.consumer_key,
                         consumer_secret=api_config.consumer_secret,
                         timeout=50)

    def raw_orders(self, status_filter):
        return self.get("orders", params={"status": status_filter,
                                          "order": "asc"}).json()

    def report(self):
        return self.get("reports").json()

    def orders(self, status_filter="processing"):
        response = self.raw_orders(status_filter)
        orders = [Order.from_json(r) for r in response]
        return orders

    def next_order(self):
        orders = self.orders()
        if len(orders) > 0:
            return orders[0]
        else:
            raise NoMoreDrinksError

    def post_note(self, order: Order, note):
        data = {
            "note": note
        }
        self.post(f"orders/{order.id}/notes", data).json()

    def update_order_status(self, order: Order, status="completed"):
        """
        pending, processing, on-hold, completed, cancelled,
        refunded, failed, trash
        """
        if status not in ["pending", "processing", "on-hold", "completed",
                          "cancelled", "refunded", "failed", "trash"]:
            raise ValueError("Incorrect status")
        data = {
            "status": status
        }
        self.put(f"orders/{order.id}", data).json()


if __name__ == "__main__":
    bar_bot = BarBotAPI()
    while True:
        print(bar_bot.next_order())
