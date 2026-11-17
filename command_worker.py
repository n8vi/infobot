"""Killable command execution: socket timeouts alone are not total deadlines."""
import asyncio
import multiprocessing
import time


def _execute(pipe, config, snapshot, cmd, args, payload, contacts):
    from core import Engine
    engine = Engine(config)
    engine.started = snapshot['started']
    engine.sources.health = snapshot['health']
    engine.sources.cache = snapshot['cache']
    engine.satellites.last_attempt = snapshot['last_attempt']
    try:
        text = engine.execute(cmd, args, payload, contacts)
        pipe.send((True, text, engine.sources.health, engine.sources.cache, engine.satellites.last_attempt))
    except Exception as error:
        pipe.send((False, str(error), engine.sources.health, engine.sources.cache, engine.satellites.last_attempt))
    finally:
        pipe.close()


async def execute_bounded(engine, cmd, args, payload, contacts, timeout, slots):
    # The deadline includes waiting for a bounded execution slot. Processes are
    # spawned (not forked from an active asyncio/network runtime) and reaped on
    # timeout, disconnect, shutdown, and ordinary completion.
    deadline = time.monotonic() + timeout
    async with asyncio.timeout(timeout):
        async with slots:
            context = multiprocessing.get_context('spawn')
            parent, child = context.Pipe(duplex=False)
            snapshot = dict(started=engine.started, health=engine.sources.health,
                            cache=engine.sources.cache, last_attempt=engine.satellites.last_attempt)
            process = context.Process(target=_execute, args=(child,engine.config,snapshot,cmd,args,payload,contacts), daemon=True)
            try:
                process.start()
                child.close()
                while not parent.poll():
                    if not process.is_alive(): raise RuntimeError('Command process exited without a result')
                    if time.monotonic() >= deadline: raise TimeoutError('Command deadline exceeded')
                    await asyncio.sleep(.02)
                ok, text, health, cache, last_attempt = parent.recv()
                engine.sources.health = health
                engine.sources.cache = cache
                engine.satellites.last_attempt = last_attempt
                if not ok: raise ValueError(text)
                return text
            finally:
                parent.close()
                child.close()
                if process.pid is not None:
                    async def reap():
                        if process.is_alive(): process.terminate()
                        await asyncio.to_thread(process.join, 1)
                        if process.is_alive():
                            process.kill()
                            await asyncio.to_thread(process.join)
                        process.close()
                    # A deadline may fire while handling an earlier error or
                    # while joining. Cleanup must finish before propagating it.
                    cleanup = asyncio.create_task(reap())
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
