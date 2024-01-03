import ctrl_process
import api_interface
import time

animation_sequence = "|/-\\"
idx = 0

try:
    while True:
        orders = api_interface.get_orders()
        ctrl_process.print_orders(orders)
        try:
            ctrl_process.process_order(orders[0])
        except IndexError:
            print(animation_sequence[idx % len(animation_sequence)], end="\r")
            idx += 1
            if idx == len(animation_sequence):
                idx = 0

        time.sleep(0.5)

except KeyboardInterrupt:
    print("Enough drinks for today...")
