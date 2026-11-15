class Cart:
    def __init__(self):
        self.items = []

    def total(self):
        return sum(self.items)


def show(cart):
    return cart.total()
