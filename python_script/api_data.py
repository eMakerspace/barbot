class Drink():
    def __init__(self, name, filler, alc):
        self.name = name
        self.filler = filler
        self.alc = alc
        self.made = False

    def __repr__(self):
        return f"{self.name:<10} ┃ {self.filler:<16} ┃ {self.alc:<10}"


class Order():
    def __init__(self, id, customer, drinks, total, status):
        self.id = id
        self.customer = customer
        self.drinks = drinks
        self.total = total
        self.status = status

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
