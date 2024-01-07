import api_interface
import ctrl_robot


def print_orders(orders):
    for order in orders:
        print(order)


def process_order(order):
    print(f"Process order {order.id}, total {order.qty} drinks")
    api_interface.update_order_status(order.id, "on-hold")
    for index, drink in enumerate(order.drinks):
        print(f"Process drink {index:>2}: {drink.name}")
        drink.made = ctrl_robot.make_drink(drink)
        api_interface.post_note(order.id, f"Finished {index} - {drink.made}")
    if all([drink.made for drink in order.drinks]):
        print(f"Order {order.id} completed, upload new status to WordPress")
        api_interface.update_order_status(order.id, "completed")
    else:
        print(f"Order {order.id} failed, upload new status to WordPress")
        api_interface.update_order_status(order.id, "failed")

    print("Upload completed")


if __name__ == "__main__":
    orders = api_interface.get_orders()
    print_orders(orders)
