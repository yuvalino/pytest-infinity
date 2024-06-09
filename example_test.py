import random
import time

import pytest


@pytest.mark.xdist_group("android")
def test_a():
    time.sleep(0.2)


@pytest.mark.xdist_group("ios")
def test_b():
    time.sleep(0.3)
    if 0 == random.randint(0, 20):
        assert False


@pytest.mark.xdist_group("android")
def test_c():
    time.sleep(0.3)


@pytest.mark.xdist_group("ios")
def test_d():
    time.sleep(0.2)
