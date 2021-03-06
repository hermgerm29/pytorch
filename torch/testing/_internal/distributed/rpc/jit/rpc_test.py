from typing import Dict, Tuple

import torch
import time
import torch.distributed as dist
import torch.distributed.rpc as rpc
from torch.distributed.rpc.internal import _build_rpc_profiling_key, RPCExecMode
from torch import Tensor
from torch.testing._internal.common_utils import TemporaryFileName
from torch.testing._internal.dist_utils import (
    dist_init,
    initialize_pg,
    worker_name,
    get_function_event
)
from torch.testing._internal.distributed.rpc.rpc_agent_test_fixture import (
    RpcAgentTestFixture,
)

def sleep(t):
    time.sleep(t)

def rpc_return_rref(dst):
    return rpc.remote(dst, torch.add, args=(torch.ones(2, 2), 1))

@torch.jit.script
def rref_local_value(rref):
    # type: (RRef[Tensor]) -> Tensor
    return rref.local_value()


def return_value(value):
    # type: (int) -> int
    return value

class RRefAPITest:
    @dist_init
    def test_rref_is_owner(self):
        dst_worker_name = worker_name((self.rank + 1) % self.world_size)
        rref_var = rpc_return_rref(dst_worker_name)

        @torch.jit.script
        def rref_tensor_is_owner(rref_var):
            # type: (RRef[Tensor]) -> bool
            return rref_var.is_owner()

        res = rref_tensor_is_owner(rref_var)
        self.assertEqual(res, False)

    @dist_init
    def test_rref_local_value(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)
        rref = rpc_return_rref(dst_worker_name)

        with self.assertRaisesRegex(RuntimeError, r"Can't call RRef.local_value\(\) on a non-owner RRef"):
            rref_local_value(rref)

        ret = ret = rpc.rpc_sync(dst_worker_name, rref_local_value, (rref,))
        self.assertEqual(ret, torch.add(torch.ones(2, 2), 1))

    @dist_init
    def test_local_rref_local_value(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name(self.rank)
        rref = rpc.remote(dst_worker_name, return_value, (5,), {})

        ret = rref_local_value(rref)
        self.assertEqual(ret, 5)

# Define Script functions on both client and server sides.
@torch.jit.script
def no_arg():
    return 0

@torch.jit.script
def one_arg(value):
    return value + 1

@torch.jit.script
def script_add_ones(x):
    return torch.add(x, torch.ones(1))

@torch.jit.script
def script_fork_wait_udf(tensor):
    fut = torch.jit._fork(script_add_ones, tensor)
    x = torch.jit._wait(fut)
    return x

@torch.jit.script
def script_raise_func(value):
    if value.numel() == 2:
        raise ValueError("Expected error")
    return value + 1

@torch.jit.script
def script_fork_wait_throw(invalue):
    fut = torch.jit._fork(script_raise_func, invalue)
    value = torch.jit._wait(fut)
    return value

@torch.jit.script
def call_rpc_with_profiling(handle: Tensor, dst_worker_name: str) -> Tensor:
    # Call rpc_async from within ScriptFunction and ensure that we can attach
    # profiling callbacks. Note that handle here is a Tensor representation of
    # RecordFunction.
    fut = rpc.rpc_async(dst_worker_name, one_arg, (torch.tensor(1),))
    torch.ops.profiler._call_end_callbacks_on_jit_fut(handle, fut)
    ret = fut.wait()
    return ret


@torch.jit.script
def call_fork_with_profiling(handle: Tensor) -> Tensor:
    # Call fork from within ScriptFunction and ensure that we can attach profiling
    # callbacks to the resulting future. Note that handle here is a Tensor
    # representation of RecordFunction.
    fut = torch.jit._fork(one_arg, torch.tensor(1))
    torch.ops.profiler._call_end_callbacks_on_jit_fut(handle, fut)
    ret = fut.wait()
    return ret


class MyScriptModuleWithRRefs(torch.jit.ScriptModule):
    def __init__(self, dst_worker):
        super().__init__()
        self.rrefs = []
        for _ in range(4):
            self.rrefs.append(rpc_return_rref(dst_worker))

    @torch.jit.script_method
    def forward(self):
        # type: () -> Tensor
        res_tensor = torch.ones(2, 2)
        for rref in self.rrefs:
            res_tensor += rref.to_here()

        return res_tensor


@torch.jit.script
class MyScriptClass:
    def __init__(self, a):
        # type: (int) -> None
        self.a = a

    def get_value(self):
        # type: () -> int
        return self.a


@torch.jit.interface
class MyModuleInterface(torch.nn.Module):
    def forward(self):
        # type: () -> Tensor
        pass


class MyScriptModule(torch.jit.ScriptModule):
    def __init__(self, rank):
        super().__init__()
        self.a = torch.ones(rank)

    @torch.jit.script_method
    def forward(self):
        # type: () -> Tensor
        return self.a


def owner_create_rref_my_script_class(a):
    return rpc.RRef(MyScriptClass(a))


def owner_create_rref_my_script_module(a):
    return rpc.RRef(MyScriptModule(a), MyModuleInterface)


@torch.jit.script
def script_run_get_value_rref_my_script_class(rref):
    # type: (RRef[MyScriptClass]) -> int
    return rref.to_here().get_value()


@torch.jit.script
def script_run_forward_rref_my_script_module(rref):
    # type: (RRef[MyModuleInterface]) -> Tensor
    return rref.to_here().forward()


class LocalRRefTest:
    @dist_init
    def test_create_local_script_class_rref_in_py(self):
        if self.rank != 0:
            return

        # Create a local RRef<MyScriptClass>.
        rref_script_class = rpc.RRef(MyScriptClass(self.rank))
        ret = rref_script_class.to_here().get_value()
        self.assertEqual(ret, self.rank)

    @dist_init
    def test_create_local_script_module_rref_in_py(self):
        if self.rank != 0:
            return

        # Create a local RRef<MyModuleInterface>.
        rref_script_module = rpc.RRef(MyScriptModule(self.rank), MyModuleInterface)
        ret = rref_script_module.to_here().forward()
        self.assertEqual(ret, torch.ones(self.rank))

        # Create a local RRef<MyModuleInterface> without type hint.
        with self.assertRaisesRegex(
            RuntimeError,
            (
                "The RRef being created contains a ScriptModule, "
                "must provide its ModuleInterface type hint."
            ),
        ):
            rref_script_module = rpc.RRef(MyScriptModule(self.rank))

    @dist_init
    def test_return_local_script_class_rref_in_py_and_use_in_script(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        # Create a local RRef<MyScripClass> remotely in Python.
        rref = rpc.rpc_sync(
            dst_worker_name, owner_create_rref_my_script_class, args=(self.rank,)
        )

        def use_rref_on_owner(rref):
            # type: (RRef[MyScriptClass]) -> int
            args = (rref,)
            kwargs: Dict[str, Any] = {}  # noqa
            fut = rpc.rpc_async(
                rref.owner(), script_run_get_value_rref_my_script_class, args, kwargs
            )
            ret = fut.wait()
            return ret

        # Use RRef<MyScripClass> in local Python RPC and remote Script run.
        ret = use_rref_on_owner(rref)
        self.assertEqual(ret, self.rank)

        # Use RRef<MyScriptClass> in local Script RPC and remote Script run.
        use_rref_on_owner_script = torch.jit.script(use_rref_on_owner)
        ret = use_rref_on_owner_script(rref)
        self.assertEqual(ret, self.rank)

    @dist_init
    def test_return_local_script_module_rref_in_py_and_use_in_script(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        # Create a local RRef<MyModuleInterface> remotely in Python.
        rref = rpc.rpc_sync(
            dst_worker_name, owner_create_rref_my_script_module, args=(self.rank,)
        )

        def use_rref_on_owner(rref):
            # type: (RRef[MyModuleInterface]) -> Tensor
            args = (rref,)
            kwargs: Dict[str, Any] = {}
            fut = rpc.rpc_async(
                rref.owner_name(),
                script_run_forward_rref_my_script_module,
                args,
                kwargs,
            )
            ret = fut.wait()
            return ret

        # Use RRef<MyScripClass> in local Python RPC and remote Script run.
        ret = use_rref_on_owner(rref)
        self.assertEqual(ret, torch.ones(self.rank))

        # Use RRef<MyScriptClass> in local Script RPC and remote Script run.
        use_rref_on_owner_script = torch.jit.script(use_rref_on_owner)
        ret = use_rref_on_owner_script(rref)
        self.assertEqual(ret, torch.ones(self.rank))


def python_function():
    return 0


@torch.jit.script
def two_args_two_kwargs(
    first_arg,
    second_arg,
    first_kwarg=torch.tensor([3, 3]),
    second_kwarg=torch.tensor([4, 4]),
):
    return first_arg + second_arg + first_kwarg + second_kwarg


@torch.jit.script
def assorted_types_args_kwargs(
    tensor_arg: Tensor,  # noqa: E999
    str_arg: str,
    int_arg: int,
    tensor_kwarg: Tensor = torch.tensor([2, 2]),
    str_kwarg: str = "str_kwarg",
    int_kwarg: int = 2,
):
    return tensor_arg + tensor_kwarg, str_arg + str_kwarg, int_arg + int_kwarg


@torch.jit.script
def raise_script():
    raise RuntimeError("Expected error")


@torch.jit.script
def rpc_async_call_remote_torchscript_in_torchscript(
    dst_worker_name: str, args: Tuple[Tensor, Tensor], kwargs: Dict[str, Tensor]
):
    fut = rpc.rpc_async(dst_worker_name, two_args_two_kwargs, args, kwargs)
    ret = fut.wait()
    return ret


class JitRpcAsyncOpTest:
    # Call functions remotely from Script.
    @dist_init
    def test_all_kwargs_are_populated_by_defaults(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        args = (torch.tensor([1, 1]), torch.tensor([2, 2]))
        kwargs = {}
        ret = rpc_async_call_remote_torchscript_in_torchscript(
            dst_worker_name, args, kwargs
        )
        self.assertEqual(ret, torch.tensor([10, 10]))

    @dist_init
    def test_some_kwargs_are_populated_by_defaults(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        args = (torch.tensor([1, 1]), torch.tensor([2, 2]))
        kwargs = {"first_kwarg": torch.tensor([2, 2])}
        ret = rpc_async_call_remote_torchscript_in_torchscript(
            dst_worker_name, args, kwargs
        )
        self.assertEqual(ret, torch.tensor([9, 9]))

    @dist_init
    def test_no_kwargs_are_populated_by_defaults(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        args = (torch.tensor([1, 1]), torch.tensor([2, 2]))
        kwargs = {
            "first_kwarg": torch.tensor([2, 2]),
            "second_kwarg": torch.tensor([3, 3]),
        }
        ret = rpc_async_call_remote_torchscript_in_torchscript(
            dst_worker_name, args, kwargs
        )
        self.assertEqual(ret, torch.tensor([8, 8]))

    @dist_init
    def test_kwargs_in_the_front_can_be_specified_by_extra_args(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        @torch.jit.script
        def rpc_async_call_remote_torchscript_in_torchscript_with_extra_arg(
            dst_worker_name: str,  # noqa: E999
        ):
            args = (
                torch.tensor([1, 1]),
                torch.tensor([2, 2]),
                # This extra arg will be fed to the first kwarg.
                torch.tensor([2, 2]),
            )
            kwargs = {"second_kwarg": torch.tensor([3, 3])}
            fut = rpc.rpc_async(dst_worker_name, two_args_two_kwargs, args, kwargs)
            ret = fut.wait()
            return ret

        ret = rpc_async_call_remote_torchscript_in_torchscript_with_extra_arg(
            dst_worker_name
        )
        self.assertEqual(ret, torch.tensor([8, 8]))

    @dist_init
    def test_args_and_kwargs_contain_different_types(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        @torch.jit.script
        def rpc_async_call_remote_torchscript_in_torchscript_with_assorted_types(
            dst_worker_name: str
        ):
            args = (torch.tensor([1, 1]), "str_arg", 1)
            # Must annotate the value type as `Any`, because JIT type inference
            # does not support multiple types when defining a Dict.
            # The error JIT gives is,
            # "Dict values must contain only a single type, "
            # "expected: Tensor but found str instead."
            kwargs: Dict[str, Any] = {
                "tensor_kwarg": torch.tensor([3, 3]),
                "str_kwarg": "_str_kwarg",
                "int_kwarg": 3,
            }
            fut = rpc.rpc_async(
                dst_worker_name, assorted_types_args_kwargs, args, kwargs
            )
            ret = fut.wait()
            return ret

        ret = rpc_async_call_remote_torchscript_in_torchscript_with_assorted_types(
            dst_worker_name
        )
        self.assertEqual(ret, (torch.tensor([4, 4]), "str_arg_str_kwarg", 4))

    @dist_init
    def test_kwargs_not_passed(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        @torch.jit.script
        def rpc_async_call_remote_torchscript_in_torchscript_without_kwargs_passed(
            dst_worker_name: str
        ):
            args = ()
            fut = rpc.rpc_async(dst_worker_name, no_arg, args)
            ret = fut.wait()
            return ret

        ret = rpc_async_call_remote_torchscript_in_torchscript_without_kwargs_passed(
            dst_worker_name
        )
        self.assertEqual(ret, 0)

    @dist_init
    def test_args_kwargs_are_neither_passed(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        @torch.jit.script
        def rpc_async_call_remote_torchscript_in_torchscript_without_args_kwargs_passed(
            dst_worker_name: str
        ):
            fut = rpc.rpc_async(dst_worker_name, no_arg)
            ret = fut.wait()
            return ret

        ret = rpc_async_call_remote_torchscript_in_torchscript_without_args_kwargs_passed(
            dst_worker_name
        )
        self.assertEqual(ret, 0)

    @dist_init
    def test_less_than_needed_args_are_specified(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        # Notice, args matching happens during scripting.
        with self.assertRaisesRegex(RuntimeError, "Argument second_arg not provided"):

            @torch.jit.script
            def rpc_async_call_remote_torchscript_in_torchscript_with_less_args(
                dst_worker_name: str,  # noqa: E999
            ):
                args = (torch.tensor([1, 1]),)
                kwargs = {}
                fut = rpc.rpc_async(dst_worker_name, two_args_two_kwargs, args, kwargs)
                ret = fut.wait()
                return ret

    @dist_init
    def test_more_than_needed_args_are_specified(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        # Notice, args matching happens during scripting.
        with self.assertRaisesRegex(
            RuntimeError,
            "Expected at most 4 arguments but found 5 positional arguments",
        ):

            @torch.jit.script
            def rpc_async_call_remote_torchscript_in_torchscript_with_more_args(
                dst_worker_name: str,
            ):
                args = (
                    torch.tensor([1, 1]),
                    torch.tensor([2, 2]),
                    torch.tensor([3, 3]),
                    torch.tensor([4, 4]),
                    torch.tensor([5, 5]),
                )
                kwargs = {}
                fut = rpc.rpc_async(dst_worker_name, two_args_two_kwargs, args, kwargs)
                ret = fut.wait()
                return ret

    @dist_init
    def test_unexepected_kwarg_is_specified(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        # Notice, kwargs matching happens during execution.
        @torch.jit.script
        def rpc_async_call_remote_torchscript_in_torchscript_with_unexpected_kwarg(
            dst_worker_name: str,  # noqa: E999
        ):
            args = (torch.tensor([1, 1]), torch.tensor([2, 2]))
            kwargs = {"third_kwarg": torch.tensor([1, 1])}
            fut = rpc.rpc_async(dst_worker_name, two_args_two_kwargs, args, kwargs)
            ret = fut.wait()
            return ret

        with self.assertRaisesRegex(
            RuntimeError, "Unknown keyword argument 'third_kwarg'"
        ):
            ret = rpc_async_call_remote_torchscript_in_torchscript_with_unexpected_kwarg(
                dst_worker_name
            )
            self.assertEqual(ret, 0)

    @dist_init
    def test_call_python_function_remotely_from_script_not_supported(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        @torch.jit.script
        def rpc_async_call_remote_py_function_in_torchscript(dst_worker_name: str):
            args = ()
            kwargs = {}
            fut = rpc.rpc_async(dst_worker_name, python_function, args, kwargs)
            ret = fut.wait()
            return ret

        with self.assertRaisesRegex(
            RuntimeError, "attempted to get undefined function"
        ):
            ret = rpc_async_call_remote_py_function_in_torchscript(dst_worker_name)
            self.assertEqual(ret, 0)

    @dist_init
    def test_call_script_function_that_raises_remotely_from_script(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        # Notice, TorchScript always translates(emits) Python `raise` statement,
        # as the exception message string, "Exception",
        # no matter what exception type and excetpion message are in the statement,
        @torch.jit.script
        def rpc_async_call_remote_raising_torchscript_in_torchscript(
            dst_worker_name: str
        ):
            args = ()
            kwargs = {}
            fut = rpc.rpc_async(dst_worker_name, raise_script, args, kwargs)
            ret = fut.wait()
            return ret

        with self.assertRaisesRegex(RuntimeError, "Exception"):
            ret = rpc_async_call_remote_raising_torchscript_in_torchscript(
                dst_worker_name
            )
            self.assertEqual(ret, 0)

    @dist_init
    def test_call_script_function_that_not_exists_remotely_from_script(self):
        if self.rank != 0:
            return

        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        @torch.jit.script
        def nonexisting_script():
            return 0

        @torch.jit.script
        def rpc_async_call_remote_nonexisting_torchscript_in_torchscript(
            dst_worker_name: str
        ):
            args = ()
            kwargs = {}
            fut = rpc.rpc_async(dst_worker_name, nonexisting_script, args, kwargs)
            ret = fut.wait()
            return ret

        with self.assertRaisesRegex(
            RuntimeError, "attempted to get undefined function nonexisting_script"
        ):
            ret = rpc_async_call_remote_nonexisting_torchscript_in_torchscript(
                dst_worker_name
            )
            self.assertEqual(ret, 0)


@torch.jit.script
def rref_to_here(rref_var):
    # type: (RRef[Tensor]) -> Tensor
    return rref_var.to_here()


@torch.jit.script
def return_rref(rref_var):
    # type: (RRef[Tensor]) -> RRef[Tensor]
    return rref_var


@torch.jit.ignore
def my_script_module_init(rank):
    # type: (int) -> MyModuleInterface
    return MyScriptModule(rank)


@torch.jit.script
def construct_my_script_module(rank):
    # type: (int) -> MyModuleInterface
    return my_script_module_init(rank)


@torch.jit.script
def run_ref_script_module(ref_script_module, t):
    # type: (RRef[MyModuleInterface], Tensor) -> Tensor
    module = ref_script_module.to_here()
    return module.forward() + t


@torch.jit.ignore
def rref_python_annotation(rref_var):
    # type: (RRef[Tensor]) -> RRef[Tensor]
    return rref_var


@torch.jit.script
def rref_script_annotation(rref_var):
    # type: (RRef[Tensor]) -> Tensor
    return rref_python_annotation(rref_var).to_here()


@torch.jit.script
def script_check_rref_confirmed(rref):
    # type: (RRef[Tensor]) -> bool
    return rref.confirmed_by_owner()


@torch.jit.script
def save_rref(rref_var, fname):
    # type: (RRef[Tensor], str) -> None
    torch.save(rref_var, fname)


class JitRpcTest(RRefAPITest, LocalRRefTest, JitRpcAsyncOpTest, RpcAgentTestFixture):
    @dist_init
    def test_torchscript_function(self):
        dst_worker_name = worker_name((self.rank + 1) % self.world_size)
        local_ret = one_arg(torch.ones(2, 2))
        ret = rpc.rpc_sync(dst_worker_name, one_arg, args=(torch.ones(2, 2),))
        self.assertEqual(ret, local_ret)
        rref = rpc.remote(dst_worker_name, one_arg, args=(torch.ones(2, 2),))
        self.assertEqual(rref.to_here(), local_ret)
        # create rref to itself
        local_rref = rpc.remote(
            worker_name(self.rank), one_arg, args=(torch.ones(2, 2),)
        )
        self.assertEqual(local_rref.to_here(), local_ret)

    @dist_init
    def test_torchscript_function_exception(self):
        dst_worker_name = worker_name((self.rank + 1) % self.world_size)
        with self.assertRaisesRegex(RuntimeError, r"one_arg\(\) expected at most"):
            ret = rpc.rpc_sync(dst_worker_name, one_arg, args=(10, 20))

        with self.assertRaisesRegex(RuntimeError, r"one_arg\(\) expected at most"):
            rref = rpc.remote(dst_worker_name, one_arg, args=(10, 20))

    @dist_init
    def test_torchscript_functions_not_supported(self):
        dst_worker_name = worker_name((self.rank + 1) % self.world_size)

        my_local_script_module = MyScriptModule(self.rank)

        # It is not thread safe to instantiate MyScriptModule in multiple threads,
        # wait for local MyScriptModule instantiation to finish,
        # otherwise it could instantiate MyScriptModule in parallel with
        # server thread in the below
        initialize_pg(self.init_method, self.rank, self.world_size)
        dist.barrier()

        # rpc_sync still accepts script class and run it in
        # the same code path as python call.
        ret = rpc.rpc_sync(dst_worker_name, MyScriptClass, args=(self.rank,))

        # rpc_sync does not accept script module and script module method.
        with self.assertRaisesRegex(RuntimeError, "ScriptModules cannot be deepcopied"):
            ret = rpc.rpc_sync(dst_worker_name, MyScriptModule, args=(self.rank,))

        # Python 3.5 and Python 3.6 throw different error message, the only
        # common word can be greped is "pickle".
        with self.assertRaisesRegex(TypeError, "pickle"):
            ret = rpc.rpc_async(
                dst_worker_name, my_local_script_module.forward, args=()
            )

    @dist_init
    def test_rref_as_arg_and_return(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        local_ret = one_arg(torch.ones(2, 2))

        # create rref on current rank
        rref = rpc.remote(worker_name(self.rank), one_arg, args=(torch.ones(2, 2),))

        # pass rref to another user in rpc call
        ret = rpc.rpc_sync(worker_name(dst_rank), rref_to_here, args=(rref,))
        self.assertEqual(ret, local_ret)

        # return rref in rpc call
        rref1 = rpc.rpc_sync(worker_name(dst_rank), return_rref, args=(rref,))
        self.assertEqual(rref1.to_here(), local_ret)

        # pass rref to another user in remote call
        rref2 = rpc.remote(worker_name(dst_rank), rref_to_here, args=(rref,))
        self.assertEqual(rref2.to_here(), local_ret)

        # return rref in remote call
        rref3 = rpc.remote(worker_name(dst_rank), return_rref, args=(rref,))
        self.assertEqual(rref3.to_here().to_here(), local_ret)

    @dist_init
    def test_remote_script_module(self):
        # TODO, need more investigation
        # there is rref leak when shutting down, suspect it is because
        # ref as arg is passed to pybind boundary, and the ref is not garbage
        # collected by python when calling shutdown()
        import torch.distributed.rpc.api as api

        api._ignore_rref_leak = True

        local_ret = torch.ones(self.rank) + torch.ones(self.rank)

        n = self.rank + 1
        dst_rank = n % self.world_size
        remote_ref = rpc.remote(
            worker_name(dst_rank), construct_my_script_module, args=(self.rank,)
        )

        # pass rref arg to owner
        ret = rpc.rpc_sync(
            worker_name(dst_rank),
            run_ref_script_module,
            args=(remote_ref, torch.ones(self.rank)),
        )
        self.assertEqual(ret, local_ret)

        # pass rref arg to self/user
        with self.assertRaisesRegex(
            RuntimeError, "is an RRef to a ScriptModule. It can't be sent through RPC from owner,"
        ):
            ret = rpc.rpc_sync(
                worker_name(self.rank),
                run_ref_script_module,
                args=(remote_ref, torch.ones(self.rank)),
            )

    @dist_init
    def test_my_script_module_with_rrefs(self):
        n = self.rank + 1
        dst_rank = n % self.world_size

        module_with_rrefs = MyScriptModuleWithRRefs(worker_name(dst_rank))
        res = module_with_rrefs()
        self.assertEqual(res, torch.ones(2, 2) * 9)

    @dist_init
    def test_rref_python_annotation(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref_var = rpc_return_rref(worker_name(dst_rank))

        res = rref_script_annotation(rref_var)
        self.assertEqual(res, torch.ones(2, 2) + 1)

    def _create_rref(self):
        owner_rank = (self.rank + 2) % self.world_size
        return rpc.remote(
            worker_name(owner_rank), torch.add, args=(torch.zeros(2, 2), 1)
        )

    @dist_init
    def test_user_rrefs_confirmed(self):
        dst_rank = (self.rank + 1) % self.world_size
        rref = self._create_rref()
        ret = rpc.rpc_sync(
            worker_name(dst_rank), script_check_rref_confirmed, args=(rref,)
        )
        self.assertEqual(ret, True)

    @dist_init
    def test_user_rrefs_confirmed_remote(self):
        dst_rank = (self.rank + 1) % self.world_size
        rref = self._create_rref()
        ret_rref = rpc.remote(
            worker_name(dst_rank), script_check_rref_confirmed, args=(rref,)
        )
        self.assertEqual(ret_rref.to_here(), True)

    @dist_init
    def test_rref_jit_pickle_not_supported(self):
        n = self.rank + 1
        dst_rank = n % self.world_size
        rref_var = rpc_return_rref(worker_name(dst_rank))
        with TemporaryFileName() as fname:
            with self.assertRaisesRegex(
                RuntimeError, "RRef jit pickling is only allowed inside RPC calls"
            ):
                save_rref(rref_var, fname)

    @dist_init
    def test_python_future_with_jit(self):
        dst_rank = (self.rank + 1) % self.world_size
        inputs = (torch.tensor([1, 1]), torch.tensor([2, 2]))
        ret_fut = rpc.rpc_async(
            worker_name(dst_rank),
            two_args_two_kwargs,
            args=inputs
        )
        expected_res = torch.tensor([10, 10])

        @torch.jit.script
        def future_wait_in_script(fut):
            # type: (Future[Tensor]) -> Tensor
            return fut.wait()

        self.assertEqual(future_wait_in_script(ret_fut), expected_res)

        @torch.jit.script
        def future_return_to_python(dst_rank, inputs):
            # type: (int, Tuple[Tensor, Tensor]) -> Future[Tensor]
            return rpc.rpc_async(
                "worker{}".format(dst_rank),
                two_args_two_kwargs,
                inputs
            )

        fut_res = future_return_to_python(dst_rank, inputs)
        self.assertEqual(fut_res.wait(), expected_res)

    @dist_init
    def test_remote_script_throw(self):
        rref = rpc.remote(worker_name((self.rank + 1) % self.world_size),
                          script_raise_func,
                          args=(torch.ones(2),))
        with self.assertRaisesRegex(Exception, ".*Expected error.*"):
            rref.to_here()

    @dist_init
    def test_remote_script_udf(self):
        rref = rpc.remote(worker_name((self.rank + 1) % self.world_size),
                          script_fork_wait_udf,
                          args=(torch.ones(2),))
        self.assertEqual(rref.to_here(), torch.ones(2) * 2)

    @dist_init
    def test_async_script_udf(self):
        future = rpc.rpc_async(
            worker_name((self.rank + 1) % self.world_size),
            script_fork_wait_udf,
            args=(torch.ones(2),))
        self.assertEqual(future.wait(), torch.ones(2) * 2)

    @dist_init
    def test_async_script_throw(self):
        future = rpc.rpc_async(
            worker_name((self.rank + 1) % self.world_size),
            script_fork_wait_throw,
            args=(torch.ones(2),))
        with self.assertRaisesRegex(Exception, ".*Expected error.*"):
            future.wait()

    @dist_init
    def test_call_rpc_with_profiling(self):
        # Ensures that we can call torch.ops.profiler._call_end_callbacks_on_jit_fut on a jit
        # future from within a script function that calls rpc_async
        if self.rank == 0:
            with torch.autograd.profiler.profile() as prof:
                prof_key = _build_rpc_profiling_key(
                    RPCExecMode.ASYNC,
                    torch.jit._qualified_name(one_arg),
                    "worker0",
                    "worker1",
                )
                with torch.autograd.profiler.record_function(prof_key) as rf:
                    ret = call_rpc_with_profiling(rf.handle, "worker1")
            # TODO: Can't get a reliable time for this profiling event since
            # it's hard to estimate the execution time on the remote end for non-UDFs.
            # This can be resolved by https://github.com/pytorch/pytorch/issues/36272.
            # After that, this test should be modified to validate the function time.
            events = prof.function_events
            function_event = get_function_event(events, prof_key)
            self.assertTrue(torch.jit._qualified_name(one_arg) in function_event.name)

    def test_record_function_jit_end_callbacks_with_fork(self):
        # Ensures that we can call rf._call_end_callbacks_on_future on a jit
        # future in python eager mode with torch.jit.fork
        sleep_interval = 1
        with torch.autograd.profiler.profile() as prof:
            with torch.autograd.profiler.record_function("foo") as rf:
                fut = torch.jit._fork(sleep, sleep_interval)
                rf._call_end_callbacks_on_future(fut)
            fut.wait()

        function_events = prof.function_events
        sleep_event = get_function_event(function_events, "foo")
        self.assertEqual(sleep_event.name, "foo")
        # Validate that callbacks were fired at the right time by checking the
        # profiling event cpu time
        self.assertGreaterEqual(sleep_event.cpu_time * 1e-6, sleep_interval)

    def test_call_fork_in_jit_with_profiling(self):
        # Ensures that we can call torch.ops.profiler._call_end_callbacks_on_jit_fut on a jit
        # future from within a script function with torch.jit.fork
        with torch.autograd.profiler.profile() as prof:
            with torch.autograd.profiler.record_function("foo") as rf:
                ret = call_fork_with_profiling(rf.handle)

        events = prof.function_events
        function_event = get_function_event(events, "foo")
        self.assertEqual(function_event.name, "foo")
