import time
from collections import Counter


class Stats:
    def __init__(self):
        self.catches = Counter()
        self.feed_sizes = {}
        self.start_time = time.time()

    def record(self, ptype):
        self.catches[ptype] += 1
