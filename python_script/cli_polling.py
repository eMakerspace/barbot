import ctrl_process
from api import BarBotAPI, NoMoreDrinksError
from ctrl_robot import Robot
import time

animation_sequence = "|/-\\"
idx = 0

bot_api = BarBotAPI()
robot = Robot()

try:
    while True:
        orders = bot_api.orders()
        ctrl_process.print_orders(orders)
        try:
            ctrl_process.process_order(bot_api.next_order(), bot_api, robot)
        except NoMoreDrinksError:
            print(animation_sequence[idx % len(animation_sequence)], end="\r")
            idx += 1
            if idx == len(animation_sequence):
                idx = 0

        time.sleep(0.5)

except KeyboardInterrupt:
    print("Enough drinks for today...")
