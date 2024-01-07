import time


def make_drink(drink):
    print("Robot: Waiting for a new cup")
    time.sleep(0.5)
    print("Robot: Start movement")
    time.sleep(0.2)
    print(f"Robot: Pouring filler: {drink.filler}")
    time.sleep(1)
    print(f"Robot: Pouring alcohol: {drink.alc}")
    time.sleep(1)
    print(f"Robot: Completed {drink.name}")

    # Simulate an error
    if drink.alc == "Whiskey":
        return False
    else:
        return True
