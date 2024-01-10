#!/usr/bin/env python3
# https://linuxconfig.org/how-to-work-with-the-woocommerce-rest-api-with-python
# https://woocommerce.github.io/woocommerce-rest-api-docs/#list-all-orders

# API Keys:
# Generate a new API key in the WooCommerce settings.
# Save these values in a config.py file inside this folder as following:
# consumer_key = "CONSUMER_KEY"
# consumer_secret = "CONSUMER_SECRET"

from woocommerce import API
import api_config
from api_data import Order, Drink
import re

wc_api = API(
  url="https://barbot.emakerspace.ch/",
  consumer_key=api_config.consumer_key,
  consumer_secret=api_config.consumer_secret,
  timeout=50
)


def get_report():
    raw_response = wc_api.get("reports")
    response = raw_response.json()
    with open("output_report.json", "w") as f:
        f.write(str(response))


def get_orders(status_filter="processing"):
    orders = []
    response = wc_api.get("orders", params={"status": status_filter,
                                            "order": "asc"}).json()
    # with open("output.json", "w") as f:
    #     f.write(str(response))
    for single_order in response:
        drinks = []
        order_id = single_order['id']
        try:
            customer = single_order["billing"]["first_name"]
            products = single_order["line_items"]
            total = single_order["total"]
            status = single_order["status"]
            for product in products:
                drink_name = product["name"]
                quantity = product["quantity"]
                filler = ""
                alc = ""
                match drink_name:
                    case "Vodka-O":
                        filler = "Orangensaft"
                        alc = "Vodka"
                    case "Rum-Cola":
                        filler = "Coca-Cola"
                        alc = "Rum"
                    case "DIY Drink":
                        meta_data = product["meta_data"]
                        config_value = meta_data[2]["display_value"]
                        pattern = re.compile(r'Filler: (.*?) Alkohol: (.*?)$')
                        match = pattern.search(config_value)
                        filler, alc = match.groups()

                drinks.extend([Drink(drink_name, filler, alc)]*quantity)

            orders.append(Order(order_id,
                                customer,
                                drinks,
                                total,
                                status))
        except (IndexError, AttributeError) as e:
            update_order_status(order_id, "failed")
            error_msg = f"Order {order_id} failed due to {e}"
            post_note(order_id, error_msg)
            print(error_msg)

    return orders


def post_note(order_id, note):
    data = {
        "note": note
    }
    wc_api.post(f"orders/{order_id}/notes", data).json()


def update_order_status(order_id, status="completed"):
    """
    pending, processing, on-hold, completed, cancelled, refunded, failed, trash
    """
    data = {
            "status": status
        }
    wc_api.put(f"orders/{order_id}", data).json()
