import time
from api import Drink
from storage import Storage, NoIngredients
from math import copysign
import sqlite3
import logging


class Robot:
    def __init__(self):
        self.storage = Storage()
        self.position = 0

    def fill_blocking(self, target):
        while self.position != target:
            self.position = self.position + copysign(1, target - self.position)
            time.sleep(0.5)
            print(f"Drive to {self.position}")
        print(f"Arrived at {target}")

    def make_drink(self, drink: Drink):
        try:
            pos_filler = self.storage.position(drink.filler, 400)
            pos_alcohol = self.storage.position(drink.alcohol, 400)
        except NoIngredients:
            logging.critical("No ingredients")
            return False
        except sqlite3.OperationalError:
            logging.critical("Database error")
            return False

        print("Robot: Waiting for a new cup")
        time.sleep(0.5)
        print("Robot: Start movement")
        time.sleep(0.2)
        print(f"Robot: Pouring filler: {drink.filler}")
        self.fill_blocking(pos_filler)
        time.sleep(1)
        print(f"Robot: Pouring alcohol: {drink.alcohol}")
        self.fill_blocking(pos_alcohol)
        time.sleep(1)
        self.fill_blocking(0)
        print(f"Robot: Completed {drink.name}")
        return True
