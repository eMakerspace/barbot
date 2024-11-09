import sqlite3
import argparse


class NoIngredients(Exception):
    pass


class Storage:
    def __init__(self, db_name='storage.db'):
        self.db_name = db_name
        self._initialize_db()

    def _initialize_db(self):
        """Create the bottles table if it doesn't exist."""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bottles (
                    position INTEGER PRIMARY KEY,
                    name TEXT,
                    amount INTEGER
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS totals (
                    name TEXT PRIMARY KEY,
                    total_amount INTEGER
                )
            ''')
            conn.commit()

    def add_bottle(self, name, pos, amount):
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bottles WHERE position = ?", (pos,))
            cursor.execute(
                """INSERT INTO bottles (position, name, amount)
                VALUES (?, ?, ?)""",
                (pos, name, amount)
            )
            conn.commit()

    def is_empty(self):
        """Check if all bottles are empty."""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT amount FROM bottles")
            return all(amount <= 0 for (amount,) in cursor.fetchall())

    def clear(self):
        """Remove all bottles from storage."""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bottles")
            conn.commit()

    def position(self, name, amount):
        """Find and update positions in storage to match the drink
        requirements."""
        amount = int(amount)
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT position, amount FROM bottles
                WHERE name = ? AND amount >= ? LIMIT 1""",
                (name, amount)
            )
            result = cursor.fetchone()

            if result:
                position, s_amount = result
                cursor.execute(
                    "UPDATE bottles SET amount = ? WHERE position = ?",
                    (s_amount - amount, position)
                )
                cursor.execute(
                    """INSERT INTO totals (name, total_amount) VALUES (?, ?)
                    ON CONFLICT(name)
                    DO UPDATE SET total_amount = total_amount + ?""",
                    (name, amount, amount)
                )

                conn.commit()
                return position
            else:
                raise NoIngredients(f"{name} {amount}")

    def at_position(self, position):
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT name, amount, position FROM bottles
                WHERE position = ? LIMIT 1""",
                (position,)
            )
            return cursor.fetchone()

    def total_amount_per_name(self):
        """Return the total amount for each type of bottle by name."""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, SUM(amount) FROM bottles GROUP BY name")
            totals = cursor.fetchall()

            return {name: total_amount for name, total_amount in totals}

    def __repr__(self):
        """Representation of the storage."""
        string = "Position ┃ Name       ┃ Amount (ml)\n"
        string += "─────────╂────────────╂────────────\n"
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT position, name, amount FROM bottles")
            for position, s_name, s_amount in cursor.fetchall():
                string += f"{position:>8} ┃ {s_name:<10} ┃ {s_amount:>11}\n"
        return string


if __name__ == "__main__":
    storage = Storage()

    parser = argparse.ArgumentParser("Bottle Storage for the BarBot")
    parser.add_argument("-b", "--bottle",
                        help="Add bottle of type")
    parser.add_argument("-p", "--position", type=int,
                        help="Get bottle at the given position")
    parser.add_argument("-o", "--overview", action="store_true",
                        help="Print the entire storage")
    parser.add_argument("-c", "--clear", action="store_true",
                        help="Clear database")
    parser.add_argument("-d", "--drink",
                        help="Name Amount")
    args = parser.parse_args()

    if args.bottle:
        name, amount, position = args.bottle.split(" ")
        storage.add_bottle(name, position, amount)
        print(storage)

    if args.drink:
        filler, alcohol = args.drink.split(" ")
        storage.position(filler, alcohol)
        print(storage)

    if args.position:
        print(storage.at_position(args.position))

    if args.clear:
        storage.clear()

    if args.overview or args is None:
        print(storage)
        print(storage.total_amount_per_name())
