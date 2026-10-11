import pytest

from rapport.llm.resident import ResidentBusy, ResidentResource


def test_warm_reuses_resource_but_never_reuses_results():
    loaded, disposed = [], []
    cache = ResidentResource(lambda: loaded.append(1) or object(), disposed.append)
    with cache.use() as (first, reused):
        assert not reused
    with cache.use() as (second, reused):
        assert reused and second is first
        with pytest.raises(ResidentBusy):
            cache.release()
        with pytest.raises(ResidentBusy):
            with cache.use():
                pass
    assert loaded == [1] and not disposed
    assert cache.release()
    assert disposed == [first]
    with cache.use() as (third, reused):
        assert third is not first and not reused
    cache.release()


def test_idle_clock_starts_after_request_and_never_unloads_in_flight():
    now, disposed = [10], []
    cache = ResidentResource(object, disposed.append, idle_seconds=30, clock=lambda: now[0])
    with cache.use():
        now[0] = 100
        with pytest.raises(ResidentBusy):
            cache.release(only_if_idle=True)
    now[0] = 129
    assert not cache.release(only_if_idle=True)
    now[0] = 130
    assert cache.release(only_if_idle=True) and len(disposed) == 1


def test_failed_load_releases_mutex_and_does_not_cache_partial_state():
    def fail():
        raise ValueError("failed")

    cache = ResidentResource(fail, lambda _: pytest.fail("No resource to dispose"))
    with pytest.raises(ValueError):
        with cache.use():
            pass
    assert not cache.info()["busy"] and not cache.info()["resident"]
    cache.load = object
    with cache.use() as (_, reused):
        assert not reused


def test_zero_idle_disposes_each_request_even_on_generation_failure():
    disposed = []
    cache = ResidentResource(object, disposed.append, idle_seconds=0)
    with pytest.raises(RuntimeError):
        with cache.use():
            raise RuntimeError("bad generation")
    assert len(disposed) == 1 and not cache.info()["resident"]


def test_cleanup_error_cannot_leave_the_request_mutex_locked():
    def fail(_):
        raise OSError("cleanup failed")

    cache = ResidentResource(object, fail, idle_seconds=0)
    with pytest.raises(OSError):
        with cache.use():
            pass
    assert not cache.info()["busy"] and not cache.info()["resident"]
