import ctrl_process
from api import BarBotAPI, NoMoreDrinksError
from ctrl_robot import Robot
import time
import logging
import datetime


animation_sequence = "|/-\\"
idx = 0

bot_api = BarBotAPI()
robot = Robot()

timestamp = datetime.datetime.now().replace(microsecond=0).isoformat()
timestamp = timestamp.replace(":", "_")
logging.basicConfig(format='%(asctime)s %(levelname)s\t%(message)s',
                    level=logging.INFO,
                    handlers=[
                        logging.FileHandler(f"{timestamp}_BARBOT.log"),
                        logging.FileHandler("global.log"),
                        logging.StreamHandler()
                    ])

try:
    while True:
        orders = bot_api.orders()
        ctrl_process.print_orders(orders)
        try:
            ctrl_process.process_order(bot_api.next_order(), bot_api, robot)
        except NoMoreDrinksError:
            logging.debug("No new order")
            print(animation_sequence[idx % len(animation_sequence)], end="\r")
            idx += 1
            if idx == len(animation_sequence):
                idx = 0

        time.sleep(0.5)

except KeyboardInterrupt:
    logging.info("Enough drinks for today...")
