class Drink():
    def __init__(self, drink_name, filler, alk):
        self.drink_name = drink_name
        self.filler = filler
        self.alk = alk
        self.made = False

    def __repr__(self):
        return f"{self.drink_name:<10} ┃ {self.filler:<16} ┃ {self.alk:<10}"


class Order():
    def __init__(self, id, prename, drinks, total, status):
        self.id = id
        self.prename = prename
        self.drinks = drinks
        self.total = total
        self.status = status

    @property
    def qty(self):
        return len(self.drinks)

    def header_only(self):
        return f"#{self.id} - {self.prename:<20}: - {self.status}\n"

    def __repr__(self):
        string = "━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━\n"
        string += f"{self.id:>6} ┃ {self.prename:<29} ┃ {self.status}\n"
        string += "━━━━━━━╋━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━╋━━━━━━━━━━━━━━\n"
        string += "    ID ┃ Name       ┃ Filler           ┃ Alk\n"
        string += "───────╂────────────╂──────────────────╂──────────────\n"
        for index, drink in enumerate(self.drinks):
            string += f"{index:>6} ┃ {drink}\n"
        return string
