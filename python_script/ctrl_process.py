from api import BarBotAPI
from ctrl_robot import Robot
import logging


def print_orders(orders):
    for order in orders:
        print(order)


def process_order(order, bar_bot: BarBotAPI, robot: Robot):
    print(f"Process order {order.id}, total {order.qty} drinks")
    bar_bot.update_order_status(order, "on-hold")
    for index, drink in enumerate(order.drinks):
        print(f"Process drink {index:>2}: {drink.name}")
        drink.made = robot.make_drink(drink)
        bar_bot.post_note(order, f"Finished {index} - {drink.made}")

    if all([drink.made for drink in order.drinks]):
        print(f"Order {order.id} completed, upload new status to WordPress")
        bar_bot.update_order_status(order, "completed")
    else:
        print(f"Order {order.id} failed, upload new status to WordPress")
        bar_bot.update_order_status(order, "failed")

    logging.info("Upload completed")


if __name__ == "__main__":
    bar_bot = BarBotAPI()
    orders = bar_bot.get_orders()
    print_orders(orders)
