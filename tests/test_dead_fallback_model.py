"""死掉的備援模型別再排：agnes-2.5-flash 在網關是 404。

逃生的四個階段每一階都會把 model 跑一輪 (None, *fallback)，404 的備援等於
每階白燒一次呼叫、拖長生成時間；20 點後 recent_errors 全是它的 NotFoundError。
"""

from app.worker import AccountWorker


def _w():
    w = AccountWorker.__new__(AccountWorker)
    w._dead_models = {}
    return w


def test_unknown_model_is_alive():
    w = _w()
    assert w._model_alive("agnes-2.5-flash") is True


def test_main_model_is_always_allowed():
    w = _w()
    w._dead_models["x"] = 99
    assert w._model_alive(None) is True


def test_two_not_founds_kill_the_model():
    w = _w()
    w._dead_models["agnes-2.5-flash"] = 2
    assert w._model_alive("agnes-2.5-flash") is False


def test_one_not_found_still_tries():
    """一次 404 可能只是暫時的，第二次才放棄。"""
    w = _w()
    w._dead_models["agnes-2.5-flash"] = 1
    assert w._model_alive("agnes-2.5-flash") is True
