"""Offline shutdown ownership tests: AST application slices, no DB/network/services."""
import ast
import asyncio
import sys
import unittest
import gzip
import json
import tempfile
import importlib.util
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]

def teardown_slice(scheduler, sequence, disabled=False, on_close=None):
    tree = ast.parse((ROOT / "backend/app/main.py").read_text())
    lifespan = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    split = next(i for i, n in enumerate(lifespan.body) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Yield))
    node = ast.AsyncFunctionDef(name="teardown", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=lifespan.body[split+1:], decorator_list=[])
    async def close():
        if on_close is not None: on_close()
        sequence.append("resource_closed")
    namespace = {"scheduler_disabled": disabled, "ai_provider": SimpleNamespace(close=close),
                 "engine": SimpleNamespace(dispose=close), "logger": Mock()}
    modules = {"app.data.scheduler": SimpleNamespace(data_scheduler=scheduler),
               "app.push.channels": SimpleNamespace(feishu_channel=SimpleNamespace(close=close))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "actual-main-shutdown", "exec"), namespace)
    return namespace["teardown"], modules

def scheduler_slice():
    tree = ast.parse((ROOT / "backend/app/data/scheduler.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DataScheduler")
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {"stop", "shutdown"}]
    namespace = {"asyncio": asyncio, "logger": Mock()}
    node = ast.ClassDef(name="Scheduler", bases=[], keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "actual-scheduler-shutdown", "exec"), namespace)
    obj = namespace["Scheduler"]()
    obj._process_awake_guard = SimpleNamespace(stop=Mock())
    obj.scheduler = SimpleNamespace(running=False)
    obj._runtime_listener_registered = False
    for name in ("_candidate_shadow_lifecycle_task", "_candidate_shadow_start_error", "_pipeline_health_task",
                 "_promotion_news_refresh_task", "_promotion_prediction_task", "_promotion_startup_catchup_task",
                 "_concept_fund_flow_task", "_index_history_task", "_limit_detail_task", "_paper_buy_point_push_task"):
        setattr(obj, name, None)
    return obj

class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scheduler = scheduler_slice()
        self.state = {"status": "not_started", "worker_alive": False}
        self.modules = {"app.news.engine": SimpleNamespace(news_engine=SimpleNamespace(cancel_pending_fetches=Mock())),
                        "app.paper.candidate_shadow": SimpleNamespace(STOP_TIMEOUT_SECONDS=5.0, candidate_shadow_status=lambda: self.state)}

    async def owner(self, release, entered):
        try: await asyncio.Event().wait()
        finally:
            entered.set(); await release.wait()
            self.state = {"status": "stopped", "worker_alive": False, "drain_complete": True}

    async def test_expected_owner_cancel_is_not_caller_cancel(self):
        release=asyncio.Event();entered=asyncio.Event()
        task=asyncio.create_task(self.owner(release,entered));self.scheduler._candidate_shadow_lifecycle_task=task
        await asyncio.sleep(0)
        try:
            with patch.dict(sys.modules,self.modules):
                caller=asyncio.create_task(self.scheduler.shutdown())
                await entered.wait();self.assertFalse(caller.done());release.set()
                result=await caller
            self.assertTrue(task.cancelled());self.assertFalse(result["complete_job_drain_proven"])
        finally:
            release.set();await asyncio.gather(task,return_exceptions=True)

    async def test_already_cancelling_not_cancelled_twice(self):
        release=asyncio.Event();entered=asyncio.Event()
        task=asyncio.create_task(self.owner(release,entered));self.scheduler._candidate_shadow_lifecycle_task=task
        await asyncio.sleep(0);task.cancel();await entered.wait();count=task.cancelling()
        with patch.dict(sys.modules,self.modules):
            caller=asyncio.create_task(self.scheduler.shutdown());await asyncio.sleep(0)
            self.assertEqual(task.cancelling(),count);release.set();await caller

    async def test_pending_startup_cancel_before_first_step(self):
        started=[]
        async def startup(): started.append(True);await asyncio.Event().wait()
        task=asyncio.create_task(startup());self.scheduler._candidate_shadow_lifecycle_task=task
        with patch.dict(sys.modules,self.modules):result=await self.scheduler.shutdown()
        self.assertEqual(started,[]);self.assertTrue(task.cancelled());self.assertEqual(result["status"],"not_started")

    async def test_started_but_pending_bindings(self):
        entered=asyncio.Event()
        async def startup(): entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(startup());self.scheduler._candidate_shadow_lifecycle_task=task;await entered.wait()
        with patch.dict(sys.modules,self.modules):self.assertEqual((await self.scheduler.shutdown())["status"],"not_started")

    async def test_done_owner_and_repeated_shutdown(self):
        async def finished():pass
        task=asyncio.create_task(finished());await task;self.scheduler._candidate_shadow_lifecycle_task=task
        with patch.dict(sys.modules,self.modules):
            await self.scheduler.shutdown();await self.scheduler.shutdown()
        self.assertEqual(task.cancelling(),0)

    async def test_disabled_main_does_not_request_stop(self):
        scheduler=SimpleNamespace(stop=Mock(side_effect=AssertionError("disabled stop")),shutdown=Mock(side_effect=AssertionError("disabled shutdown")))
        sequence=[];fn,modules=teardown_slice(scheduler,sequence,disabled=True)
        with patch.dict(sys.modules,modules):await fn()
        self.assertEqual(len(sequence),3)

    async def test_timeout_preserves_owner_finalizer(self):
        release=asyncio.Event();entered=asyncio.Event()
        task=asyncio.create_task(self.owner(release,entered));self.scheduler._candidate_shadow_lifecycle_task=task;await asyncio.sleep(0)
        try:
            with patch.dict(sys.modules,self.modules):
                with self.assertRaisesRegex(TimeoutError,"drain incomplete"):
                    await self.scheduler.shutdown(shadow_timeout_seconds=.02)
            self.assertFalse(task.done());self.assertEqual(task.cancelling(),1)
        finally:release.set();await asyncio.gather(task,return_exceptions=True)

    async def test_caller_cancellation_drains_then_propagates_without_recancel(self):
        release=asyncio.Event();entered=asyncio.Event()
        task=asyncio.create_task(self.owner(release,entered));self.scheduler._candidate_shadow_lifecycle_task=task;await asyncio.sleep(0)
        with patch.dict(sys.modules,self.modules):
            caller=asyncio.create_task(self.scheduler.shutdown());await entered.wait()
            caller.cancel();await asyncio.sleep(0);caller.cancel();await asyncio.sleep(0)
            self.assertFalse(caller.done());self.assertEqual(task.cancelling(),1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):await caller
        self.assertTrue(task.done());self.assertEqual(self.state["status"],"stopped")

    async def test_cancelled_caller_timeout_remains_bounded(self):
        release=asyncio.Event();entered=asyncio.Event()
        task=asyncio.create_task(self.owner(release,entered));self.scheduler._candidate_shadow_lifecycle_task=task;await asyncio.sleep(0)
        try:
            with patch.dict(sys.modules,self.modules):
                caller=asyncio.create_task(self.scheduler.shutdown(shadow_timeout_seconds=.02));await entered.wait();caller.cancel()
                with self.assertRaises(asyncio.CancelledError) as raised:await caller
                self.assertIsInstance(raised.exception.__cause__,TimeoutError)
            self.assertFalse(task.done());self.assertEqual(task.cancelling(),1)
        finally:release.set();await asyncio.gather(task,return_exceptions=True)

    async def test_finalizer_error_propagates(self):
        async def failed():raise ValueError("fake finalizer")
        task=asyncio.create_task(failed());await asyncio.sleep(0);self.scheduler._candidate_shadow_lifecycle_task=task
        with patch.dict(sys.modules,self.modules):
            with self.assertRaisesRegex(ValueError,"fake finalizer"):await self.scheduler.shutdown()

    async def test_failed_lifecycle_status_not_success(self):
        self.scheduler._candidate_shadow_start_error="fake"
        with patch.dict(sys.modules,self.modules):
            with self.assertRaisesRegex(RuntimeError,"incomplete or failed"):await self.scheduler.shutdown()

    async def test_done_owner_but_live_or_incomplete_worker_rejected(self):
        for state in ({"status":"running","worker_alive":True},{"status":"stopped","drain_complete":False},
                      {"status":"drain_incomplete","stop_timed_out":True}):
            self.state=state
            with patch.dict(sys.modules,self.modules):
                with self.assertRaisesRegex(RuntimeError,"incomplete or failed"):await self.scheduler.shutdown()

    async def test_invalid_timeout_rejected(self):
        with patch.dict(sys.modules,self.modules):
            for value in (0,-1,float("inf"),float("nan"),True,99):
                with self.assertRaises(ValueError):await self.scheduler.shutdown(shadow_timeout_seconds=value)

    async def test_main_closes_resources_on_shutdown_timeout(self):
        await self.check_main_closes_on_error(TimeoutError("synthetic shutdown timeout"))

    async def test_main_closes_resources_on_shutdown_worker_error(self):
        await self.check_main_closes_on_error(RuntimeError("synthetic worker error"))

    async def test_main_closes_resources_on_caller_cancelled_error(self):
        await self.check_main_closes_on_error(asyncio.CancelledError("synthetic caller cancel"))

    async def check_main_closes_on_error(self, error):
        async def shutdown(): raise error
        sequence=[];fn,modules=teardown_slice(SimpleNamespace(shutdown=shutdown),sequence)
        with patch.dict(sys.modules,modules):
            with self.assertRaises(type(error)) as caught: await fn()
        self.assertIs(caught.exception,error)
        self.assertEqual(sequence,["resource_closed"]*3)

    async def test_repeated_caller_cancel_does_not_renew_deadline(self):
        release=asyncio.Event();entered=asyncio.Event()
        task=asyncio.create_task(self.owner(release,entered));self.scheduler._candidate_shadow_lifecycle_task=task;await asyncio.sleep(0)
        clock=[0.0];timeouts=[]
        async def interrupted_wait(tasks,timeout):
            self.assertEqual(tasks,{task});timeouts.append(timeout);clock[0]+=1
            raise asyncio.CancelledError()
        facade=SimpleNamespace(get_running_loop=lambda:SimpleNamespace(time=lambda:clock[0]),
                               current_task=asyncio.current_task,wait=interrupted_wait,CancelledError=asyncio.CancelledError)
        namespace=self.scheduler.shutdown.__func__.__globals__
        try:
            with patch.dict(sys.modules,self.modules),patch.dict(namespace,{"asyncio":facade}):
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await self.scheduler.shutdown(shadow_timeout_seconds=3)
            self.assertEqual(timeouts,[3,2,1]);self.assertIsInstance(caught.exception.__cause__,TimeoutError)
            self.assertEqual(task.cancelling(),1)
        finally:release.set();await asyncio.gather(task,return_exceptions=True)

    async def test_real_temp_worker_publishes_stop_before_main_dispose(self):
        from app.paper import candidate_shadow as shadow
        old=shadow._runtime
        self.assertIsNone(old,"isolated test must not adopt an existing worker")
        clock=[datetime(2026,9,28,23,59,59)]
        with tempfile.TemporaryDirectory(prefix="shadow-shutdown-") as directory, patch.object(shadow,"_now",side_effect=lambda:clock[0]):
            directory = Path(directory).resolve()  # macOS /var is a symlink; publisher rejects symlink ancestors.
            await shadow.start_candidate_shadow(output_dir=directory,bindings={})
            try:
                for _ in range(200):
                    if shadow.candidate_shadow_status().get("counts",{}).get("durable_records")==1:break
                    await asyncio.sleep(.005)
                status = shadow.candidate_shadow_status()
                self.assertEqual(status["counts"].get("durable_records"),1,repr(status))
                clock[0]=datetime(2026,9,29,0,0,1)
                async def owner():
                    try:await asyncio.Event().wait()
                    finally:await shadow.stop_candidate_shadow()
                task=asyncio.create_task(owner());self.scheduler._candidate_shadow_lifecycle_task=task;await asyncio.sleep(0)
                def assert_durable_before_resource_close():
                    state=shadow.candidate_shadow_status()
                    self.assertFalse(state["worker_alive"]);self.assertTrue(state["drain_complete"])
                    durable=[r for p in Path(directory).glob("*/*.json.gz") for r in json.loads(gzip.decompress(p.read_bytes()))["records"]]
                    self.assertEqual(sum(r["kind"]=="stop_boundary" for r in durable),1)
                sequence=[];fn,modules=teardown_slice(self.scheduler,sequence,on_close=assert_durable_before_resource_close)
                modules.update({"app.news.engine":self.modules["app.news.engine"]})
                with patch.dict(sys.modules,modules):await fn()
                status=shadow.candidate_shadow_status();self.assertTrue(status["drain_complete"]);self.assertFalse(status["worker_alive"])
                records=[row for p in Path(directory).glob("*/*.json.gz") for row in json.loads(gzip.decompress(p.read_bytes()))["records"]]
                stops=[r for r in records if r["kind"]=="stop_boundary"];starts=[r for r in records if r["kind"]=="restart_boundary"]
                self.assertEqual(len(stops),1);self.assertEqual(len(starts),1)
                self.assertEqual(starts[0]["trade_date"],"2026-09-28");self.assertEqual(stops[0]["trade_date"],"2026-09-29")
                self.assertEqual(stops[0]["session_id"],starts[0]["session_id"]);self.assertTrue(task.done())
            finally:
                await shadow.stop_candidate_shadow();shadow._runtime=old

    async def test_actual_lifespan_waits_for_finalizer_before_dispose(self):
        sequence = []; release = asyncio.Event(); stopping = asyncio.Event()
        async def owner():
            try: await asyncio.Event().wait()
            finally:
                stopping.set(); await release.wait(); sequence.append("shadow_finalized")
        task = asyncio.create_task(owner()); await asyncio.sleep(0)
        def stop():
            if not task.done() and not task.cancelling(): task.cancel()
        async def shutdown():
            stop()
            try: await task
            except asyncio.CancelledError: pass
        teardown, modules = teardown_slice(SimpleNamespace(stop=stop, shutdown=shutdown), sequence)
        pending = None
        try:
            with patch.dict(sys.modules, modules):
                pending = asyncio.create_task(teardown())
                await asyncio.wait_for(stopping.wait(), 1)
                await asyncio.sleep(0)
                self.assertFalse(pending.done(), "lifespan returned before shadow finalizer drained")
                self.assertNotIn("resource_closed", sequence)
                release.set(); await pending
            self.assertEqual(sequence[0], "shadow_finalized")
        finally:
            release.set()
            if pending is not None: await asyncio.gather(pending, return_exceptions=True)
            await asyncio.gather(task, return_exceptions=True)

if __name__ == "__main__": unittest.main()
