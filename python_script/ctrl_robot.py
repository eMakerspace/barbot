import time
from api import Drink
from storage import Storage, NoIngredients
from math import copysign
import sqlite3
import logging
import serial


class Robot:
    def __init__(self):
        self.storage = Storage()
        self.position = 0
        self._ser = serial.Serial("COM29", timeout=1)

    def fill_non_blocking(self, target, amount):
        self._ser.write((f"G1 X{target} Z{amount}\n").encode('utf-8'))

    def move_non_blocking(self, target):
        self._ser.write((f"G1 X{target}\n").encode('utf-8'))

    def wait_for_idle(self):
        self._ser.write(("STATUS").encode('utf-8'))
        response = ""
        while response != "idle":
            response = self._ser.read_until(expected=b"idle")

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
        print("Robot: Start movement")
        print(f"Robot: Pouring filler: {drink.filler}")
        self.fill_non_blocking(pos_filler, 3000)
        print(f"Robot: Pouring alcohol: {drink.alcohol}")
        self.fill_non_blocking(pos_alcohol, 2000)
        # self.wait_for_idle()
        print(f"Robot: Completed {drink.name}")
        return True
