"""
tk_gc.py

Keeps Python's cyclic garbage collection on Tk's thread while a worker
thread runs.

A collection can be triggered by whichever thread happens to allocate. If a
worker thread triggers one, leftover Tk objects (StringVars, PhotoImages of a
closed dialog, ...) are finalized on that thread, and their __del__ calls Tk,
which only works from Tk's thread: the console fills with "Exception ignored
in Variable.__del__ ... RuntimeError: main thread is not in main loop".

Usage, from Tk's thread:

    tk_gc.pause()              # before starting the worker
    ... in the after() poll:    tk_gc.tick()
    tk_gc.resume()             # once the worker has finished

Pauses nest (a counter), so overlapping workers are fine.
"""
import gc
import threading

_lock = threading.Lock()
_depth = 0


def pause():
    """Collect now, then turn automatic collection off until resume()."""
    global _depth
    with _lock:
        if _depth == 0:
            gc.collect()
            gc.disable()
        _depth += 1


def tick():
    """Collect young garbage on Tk's thread (call from a poll loop)."""
    gc.collect(0)


def resume():
    """Undo one pause(); the last one turns collection back on."""
    global _depth
    with _lock:
        if _depth == 0:
            return
        _depth -= 1
        if _depth == 0:
            gc.enable()
            gc.collect()
