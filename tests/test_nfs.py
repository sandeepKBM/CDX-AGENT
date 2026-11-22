from cdx_agent.nfs import wait_visible


class Fake:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0
        self.on_sleep = None

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def test_exists_immediately(tmp_path):
    f = tmp_path / "x"
    f.write_text("1")
    fk = Fake()
    assert wait_visible(f, 5, clock=fk.clock, sleep=fk.sleep) is True
    assert fk.sleeps == 0


def test_appears_after_sleeps(tmp_path):
    f = tmp_path / "x"
    fk = Fake()
    fk.on_sleep = lambda n: f.write_text("1") if n == 3 else None
    assert wait_visible(f, 10, 1.0, clock=fk.clock, sleep=fk.sleep) is True
    assert fk.sleeps == 3


def test_timeout(tmp_path):
    fk = Fake()
    assert wait_visible(tmp_path / "no", 3, 1.0, clock=fk.clock, sleep=fk.sleep) is False
    assert fk.sleeps == 3


def test_zero_timeout_one_attempt(tmp_path):
    fk = Fake()
    assert wait_visible(tmp_path / "no" / "deeper", 0, clock=fk.clock, sleep=fk.sleep) is False
    assert fk.sleeps == 0
