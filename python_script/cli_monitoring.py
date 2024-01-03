import time
import api_interface


def clear_line(n=1):
    LINE_UP = '\033[1A'
    LINE_CLEAR = '\x1b[2K'
    for i in range(n):
        print(LINE_UP, end=LINE_CLEAR)


def get_sum(orders):
    sum_per_status = dict()
    for order in orders:
        current_sum = sum_per_status.get(order.status, 0)
        sum_per_status.update({order.status: current_sum + order.qty})
    return sum_per_status


api_interface.get_report()

try:
    while True:
        orders = api_interface.get_orders(status_filter="any")
        sums = get_sum(orders)
        for key, value in sums.items():
            print(f"{key:<10}: {value:<5}")
        time.sleep(1)
        clear_line(len(sums))
except KeyboardInterrupt:
    pass
