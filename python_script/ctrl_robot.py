import time
from api import Drink


class Storage:
    def __init__(self):
        self.bottles = {}

    def add_bottle(self, name, position, amount):
        if position in self.bottles:
            raise KeyError("Position already filled")
        else:
            self.bottles[position] = (name, amount)

    def is_empty(self):
        """Check if all bottles are empty."""
        return all(amount <= 0 for (_, amount) in self.bottles.values())

    def positions(self, drink: Drink):
        positions = [None, None]
        for index, (t_name, t_amount) in enumerate(zip([drink.filler,
                                                        drink.alcohol],
                                                       [400, 40])):
            for position, (s_name, s_amount) in self.bottles.items():
                if s_name == t_name and s_amount > t_amount:
                    positions[index] = position
                    self.bottles[position] = (s_name, s_amount - t_amount)
                    break
        if None in positions:
            raise ValueError("Not enough supplies!")

        return positions

    def __repr__(self):
        # string = "━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━\n"
        string = "Position ┃ Name       ┃ Amount (ml)\n"
        string += "─────────╂────────────╂────────────\n"
        for position, (s_name, s_amount) in self.bottles.items():
            string += f"{position:>8} ┃ {s_name:<10} ┃ {s_amount:>11}\n"
        return string


storage = Storage()
storage.add_bottle("O-Saft", 0, 2000)
storage.add_bottle("O-Saft", 1, 2000)
storage.add_bottle("Cola", 2, 2000)
storage.add_bottle("Cola", 3, 2000)
storage.add_bottle("Whisky", 4, 1000)
storage.add_bottle("Rum", 5, 1000)
storage.add_bottle("Vodka", 6, 1000)
print(storage.is_empty())

drink = Drink("Vodka-O", "O-Saft", "Vodka")
print(storage.positions(drink))
print(storage.positions(drink))
print(storage.positions(drink))
print(storage.positions(drink))
print(storage.positions(drink))
print(storage.positions(drink))
print(storage.positions(drink))
print(storage.positions(drink))

print(storage)


class Robot:
    def __init__(self):
        pass

    def make_drink(self, drink: Drink):
        print("Robot: Waiting for a new cup")
        time.sleep(0.5)
        print("Robot: Start movement")
        time.sleep(0.2)
        print(f"Robot: Pouring filler: {drink.filler}")
        time.sleep(1)
        print(f"Robot: Pouring alcohol: {drink.alcohol}")
        time.sleep(1)
        print(f"Robot: Completed {drink.name}")

        # Simulate an error
        if drink.alcohol == "Whisky":
            return False
        else:
            return True
