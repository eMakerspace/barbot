import api_interface
import ctrl_roboter


def print_orders(orders):
    for order in orders:
        print(order)


def process_order(order):
    print(f"Process order {order.id}")
    api_interface.update_order_status(order.id, "on-hold")
    for index, drink in enumerate(order.drinks):
        print(f"Process drink {index:>2}/{order.qty-1:>2}: {drink.drink_name}")
        success = ctrl_roboter.make_drink(drink.drink_name)
        if success:
            api_interface.post_note(order.id, f"Finished {index}")
            drink.made = True
    print(f"Finish order {order.id}, upload new status to WordPress")
    api_interface.update_order_status(order.id, "completed")
    print("Finish uploading")


if __name__ == "__main__":
    orders = api_interface.get_orders()
    print_orders(orders)
