#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import abc
import contextlib
import logging
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    cast,
    ContextManager,
    Deque,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

import torch
import torchrec.distributed.comm_ops
from torch.autograd.profiler import record_function
from torchrec.distributed.dist_data import KJTAllToAllTensorsAwaitable
from torchrec.distributed.model_parallel import ShardedModule
from torchrec.distributed.train_pipeline.utils import (
    _override_input_dist_forwards,
    _pipeline_detach_model,
    _prefetch_embeddings,
    _rewrite_model,
    _start_data_dist,
    _start_embedding_lookup,
    _to_device,
    _wait_for_batch,
    _wait_for_events,
    DataLoadingThread,
    EmbeddingPipelinedForward,
    EmbeddingTrainPipelineContext,
    In,
    Out,
    PipelinedForward,
    PipelinedPreproc,
    PipelineStage,
    PrefetchPipelinedForward,
    PrefetchTrainPipelineContext,
    RunnableType,
    StageOut,
    StageOutputWithEvent,
    TrainPipelineContext,
)
from torchrec.distributed.types import Awaitable
from torchrec.pt2.checks import is_torchdynamo_compiling
from torchrec.pt2.utils import default_pipeline_input_transformer
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor
from torchrec.streamable import Pipelineable

logger: logging.Logger = logging.getLogger(__name__)

# This is required to support older torch package export for older models
try:
    from torchrec.distributed.comm_ops import torchrec_use_sync_collectives
except ImportError:
    logger.warning("torchrec_use_sync_collectives is not available")

if not torch._running_with_deploy():
    torch.ops.import_module("fbgemm_gpu.sparse_ops")


class ModelDetachedException(Exception):
    pass


class TrainPipeline(abc.ABC, Generic[In, Out]):
    @abc.abstractmethod
    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        pass


@dataclass
class TorchCompileConfig:
    """
    Configs for torch.compile

    fullgraph: bool = False, whether to compile the whole graph or not
    dynamic: Optional[bool] = None, whether to use dynamic shapes or not, if None, automatic_dynamic_shapes will apply
    backend: str = "inductor", which compiler to use (either inductor or aot)
    compile_on_iter: int = 3, compile the model on which iteration
        this is useful when we want to profile the first few iterations of training
        and then start using compiled model from iteration #3 onwards
    """

    fullgraph: bool = False
    dynamic: Optional[bool] = None
    backend: str = "inductor"
    compile_on_iter: int = 3


class TrainPipelineBase(TrainPipeline[In, Out]):
    """
    This class runs training iterations using a pipeline of two stages, each as a CUDA
    stream, namely, the current (default) stream and `self._memcpy_stream`. For each
    iteration, `self._memcpy_stream` moves the input from host (CPU) memory to GPU
    memory, and the default stream runs forward, backward, and optimization.
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        device: torch.device,
        custom_model_fwd: Optional[
            Callable[[In], Tuple[torch.Tensor, List[torch.Tensor]]]
        ] = None,
    ) -> None:
        self._model = model
        self._optimizer = optimizer
        self._device = device
        self._memcpy_stream: Optional[torch.Stream] = (
            torch.get_device_module(device).Stream()
            if device.type in ["cuda", "mtia"]
            else None
        )

        # pyre-ignore
        self._stream_context = (
            torch.get_device_module(self._device).stream
            if self._device.type in ["cuda", "mtia"]
            else torch.cuda.stream
        )
        self._cur_batch: Optional[In] = None
        self._connected = False

    def _connect(self, dataloader_iter: Iterator[In]) -> None:
        cur_batch = next(dataloader_iter)
        self._cur_batch = cur_batch
        with self._stream_context(self._memcpy_stream):
            self._cur_batch = _to_device(cur_batch, self._device, non_blocking=True)
        self._connected = True

    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        if not self._connected:
            self._connect(dataloader_iter)

        # Fetch next batch
        with record_function("## next_batch ##"):
            next_batch = next(dataloader_iter)
        cur_batch = self._cur_batch
        assert cur_batch is not None

        if self._model.training:
            with record_function("## zero_grad ##"):
                self._optimizer.zero_grad()

        with record_function("## wait_for_batch ##"):
            _wait_for_batch(cur_batch, self._memcpy_stream)

        with record_function("## forward ##"):
            (losses, output) = self._model(cur_batch)

        if self._model.training:
            with record_function("## backward ##"):
                torch.sum(losses, dim=0).backward()

        # Copy the next batch to GPU
        self._cur_batch = cur_batch = next_batch
        with record_function("## copy_batch_to_gpu ##"):
            with self._stream_context(self._memcpy_stream):
                self._cur_batch = _to_device(cur_batch, self._device, non_blocking=True)

        # Update
        if self._model.training:
            with record_function("## optimizer ##"):
                self._optimizer.step()

        return output


class TrainPipelinePT2(TrainPipelineBase[In, Out]):
    """
    This pipeline uses PT2 compiler to compile the model and run it in a single stream (default)
    Args:
        model (torch.nn.Module): model to pipeline.
        optimizer (torch.optim.Optimizer): optimizer to use.
        device (torch.device): device where the model is run
        compile_configs (TorchCompileConfig): configs for compling the model
        pre_compile_fn (Callable[[torch.nn.Module], [None]]): Optional callable to execute before compiling the model
        post_compile_fn (Callable[[torch.nn.Module], [None]]): Optional callable to execute after compiling the model
        input_transformer (Callable[[In], In]): transforms the input before passing it to the model.
            This is useful when we want to transform KJT parameters for PT2 tracing
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        device: torch.device,
        compile_configs: Optional[TorchCompileConfig] = None,
        pre_compile_fn: Optional[Callable[[torch.nn.Module], None]] = None,
        post_compile_fn: Optional[Callable[[torch.nn.Module], None]] = None,
        input_transformer: Optional[Callable[[In], In]] = None,
    ) -> None:
        self._model = model
        self._optimizer = optimizer
        self._device = device
        self._compile_configs: TorchCompileConfig = (
            compile_configs or TorchCompileConfig()
        )
        self._pre_compile_fn = pre_compile_fn
        self._post_compile_fn = post_compile_fn
        # pyre-ignore
        self._input_transformer = (
            input_transformer or default_pipeline_input_transformer
        )
        self._iter = 0
        self._cur_batch: Optional[In] = None

    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        if self._iter == 0:
            # Turn on sync collectives for PT2 pipeline.
            # To have similar logic between compiled/graph_break ranks.
            # TODO(ivankobzarev): Call torchrec.distributed.comm_ops.set_use_sync_collectives(True) when torch package issue on import of comm_ops is fixed
            pass

        cc = self._compile_configs

        with record_function("## load_batch ##"):
            cur_batch = next(dataloader_iter)

        with record_function("## copy_batch_to_gpu ##"):
            self._cur_batch = _to_device(cur_batch, self._device, non_blocking=False)

        # Input transformer here is used also for pt2 hints to compiler, that should happen on exact object passed to model.compile.
        # Do not move it before _to_device
        if self._input_transformer:
            self._cur_batch = self._input_transformer(self._cur_batch)

        if self._model.training:
            with record_function("## zero_grad ##"):
                self._optimizer.zero_grad()

        with record_function("## forward ##"):
            if self._iter == cc.compile_on_iter:
                logger.info("Compiling model...")
                if self._pre_compile_fn:
                    self._pre_compile_fn(self._model)

                # Mandatory dynamo configuration for Torchrec PT2 compilation
                torch._dynamo.config.capture_scalar_outputs = True
                torch._dynamo.config.capture_dynamic_output_shape_ops = True
                torch._dynamo.config.force_unspec_int_unbacked_size_like_on_torchrec_kjt = (
                    True
                )
                torch._dynamo.config.skip_torchrec = False

                # Importing only before compilation to not slow-done train_pipelines import
                torch.ops.import_module("fbgemm_gpu.sparse_ops")

                self._model.compile(
                    fullgraph=cc.fullgraph, dynamic=cc.dynamic, backend=cc.backend
                )
                if self._post_compile_fn:
                    self._post_compile_fn(self._model)

            (losses, output) = self._model(self._cur_batch)
            self._iter += 1

        if self._model.training:
            with record_function("## backward ##"):
                torch.sum(losses).backward()

            with record_function("## optimizer ##"):
                self._optimizer.step()

        return output


class TrainPipelineSparseDist(TrainPipeline[In, Out]):
    """
    This pipeline overlaps device transfer, and `ShardedModule.input_dist()` with
    forward and backward. This helps hide the all2all latency while preserving the
    training forward / backward ordering.

    stage 3: forward, backward - uses default CUDA stream
    stage 2: ShardedModule.input_dist() - uses data_dist CUDA stream
    stage 1: device transfer - uses memcpy CUDA stream

    `ShardedModule.input_dist()` is only done for top-level modules in the call graph.
    To be considered a top-level module, a module can only depend on 'getattr' calls on
    input.

    Input model must be symbolically traceable with the exception of `ShardedModule` and
    `DistributedDataParallel` modules.

    Args:
        model (torch.nn.Module): model to pipeline.
        optimizer (torch.optim.Optimizer): optimizer to use.
        device (torch.device): device where device transfer, sparse data dist, and
            forward/backward pass will happen.
        execute_all_batches (bool): executes remaining batches in pipeline after
            exhausting dataloader iterator.
        apply_jit (bool): apply torch.jit.script to non-pipelined (unsharded) modules.
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        device: torch.device,
        execute_all_batches: bool = True,
        apply_jit: bool = False,
        context_type: Type[TrainPipelineContext] = TrainPipelineContext,
        pipeline_preproc: bool = False,
        custom_model_fwd: Optional[
            Callable[[Optional[In]], Tuple[torch.Tensor, Out]]
        ] = None,
    ) -> None:
        self._model = model
        self._optimizer = optimizer
        self._device = device
        self._execute_all_batches = execute_all_batches
        self._apply_jit = apply_jit

        if device.type == "cuda":
            # use two data streams to support two concurrent batches
            # Dynamo does not support cuda stream specificaiton,
            # this freedom is left for compiler pipelining optimizations.
            assert (
                not is_torchdynamo_compiling()
            ), "Train Pipelines rely on cuda streams, which is not supported by Dynamo"

        # pyre-ignore
        self._stream_context = (
            torch.get_device_module(self._device).stream
            if self._device.type in ["cuda", "mtia"]
            else torch.cuda.stream
        )

        self._memcpy_stream: Optional[torch.Stream] = (
            (torch.get_device_module(device).Stream(priority=-1))
            if device.type in ["cuda", "mtia"]
            else None
        )
        self._data_dist_stream: Optional[torch.Stream] = (
            (torch.get_device_module(device).Stream(priority=-1))
            if device.type in ["cuda", "mtia"]
            else None
        )

        # pyre-ignore
        self._original_forwards: List[Callable[..., Any]] = []

        self._original_kjt_dist_forwards: List[
            Callable[[KeyedJaggedTensor], Awaitable[KJTAllToAllTensorsAwaitable]]
        ] = []

        self._model_attached = True
        self._pipeline_preproc = pipeline_preproc

        self._next_index: int = 0
        self.contexts: Deque[TrainPipelineContext] = deque()
        self._pipelined_modules: List[ShardedModule] = []
        self._pipelined_preprocs: List[PipelinedPreproc] = []
        self.batches: Deque[Optional[In]] = deque()
        self._dataloader_iter: Optional[Iterator[In]] = None
        self._dataloader_exhausted: bool = False
        self._context_type: Type[TrainPipelineContext] = context_type

        self._model_fwd: Callable[[Optional[In]], Tuple[torch.Tensor, Out]] = (
            custom_model_fwd if custom_model_fwd else model
        )

        # DEPRECATED FIELDS
        self._batch_i: Optional[In] = None
        self._batch_ip1: Optional[In] = None
        self._batch_ip2: Optional[In] = None
        self._context: TrainPipelineContext = context_type(version=0)

    def detach(self) -> torch.nn.Module:
        """
        Detaches the model from sparse data dist (SDD) pipeline.
        To use the pipeline after detaching the model, pipeline.attach(model)
        needs to be called.
        Inflight batches are kept so pipeline.progress(data_iter) can be resumed normally.

        Returns the original model.
        """
        if self._pipelined_modules:
            _pipeline_detach_model(
                pipelined_modules=self._pipelined_modules,
                original_forwards=self._original_forwards,
                original_kjt_dist_forwards=self._original_kjt_dist_forwards,
            )

        self._model_attached = False
        return self._model

    def attach(self, model: Optional[torch.nn.Module] = None) -> None:
        if model:
            self._model = model

        self._model_attached = True
        if self.contexts:
            self._pipeline_model(
                batch=self.batches[0],
                context=self.contexts[0],
                pipelined_forward=PipelinedForward,
            )
        else:
            # attaching the model after end of train pipeline
            # model rewrite for SDD needs context but self.contexts is empty
            # reset _pipelined_modules so _fill_pipeline will rewrite model on progress()
            self._pipelined_modules = []

    def _set_module_context(self, context: TrainPipelineContext) -> None:
        for module in self._pipelined_modules:
            module.forward.set_context(context)

        for preproc_module in self._pipelined_preprocs:
            # This ensures that next iter model fwd uses cached results
            preproc_module.set_context(context)

    def enqueue_batch(self, dataloader_iter: Iterator[In]) -> bool:
        batch, context = self.copy_batch_to_gpu(dataloader_iter)
        if batch is None:
            return False
        self.batches.append(batch)
        # pyre-ignore [6]
        self.contexts.append(context)

        return True

    def dequeue_batch(self) -> None:
        self.batches.popleft()
        self.contexts.popleft()
        # update PipelineForwards context to match next forward pass
        if len(self.batches) >= 1:
            self._set_module_context(self.contexts[0])

    def fill_pipeline(self, dataloader_iter: Iterator[In]) -> None:
        # pipeline is already filled
        if len(self.batches) >= 2:
            return
        # executes last batch in pipeline
        if self.batches and self._execute_all_batches:
            return

        # batch i
        if not self.enqueue_batch(dataloader_iter):
            return

        self._init_pipelined_modules(
            # pyre-ignore [6]
            self.batches[0],
            self.contexts[0],
            PipelinedForward,
        )
        self.wait_sparse_data_dist(self.contexts[0])

        # batch i+1
        if not self.enqueue_batch(dataloader_iter):
            return

    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        if not self._model_attached:
            self.attach(self._model)

        self.fill_pipeline(dataloader_iter)
        if not self.batches:
            raise StopIteration

        # TODO: Remove once Bulk Eval migrated (needed for bwd compat, this class only)
        self._set_module_context(self.contexts[0])

        if self._model.training:
            with record_function("## zero_grad ##"):
                self._optimizer.zero_grad()

        with record_function("## wait_for_batch ##"):
            _wait_for_batch(cast(In, self.batches[0]), self._data_dist_stream)

        if len(self.batches) >= 2:
            self.start_sparse_data_dist(self.batches[1], self.contexts[1])

        # batch i+2
        self.enqueue_batch(dataloader_iter)

        # forward
        with record_function("## forward ##"):
            losses, output = self._model_fwd(self.batches[0])

        if len(self.batches) >= 2:
            self.wait_sparse_data_dist(self.contexts[1])

        if self._model.training:
            # backward
            with record_function("## backward ##"):
                torch.sum(losses, dim=0).backward()

            # update
            with record_function("## optimizer ##"):
                self._optimizer.step()

        self.dequeue_batch()
        return output

    def _create_context(self) -> TrainPipelineContext:
        context = self._context_type(index=self._next_index, version=1)
        self._next_index += 1
        return context

    def _pipeline_model(
        self,
        batch: Optional[In],
        context: TrainPipelineContext,
        pipelined_forward: Type[PipelinedForward] = PipelinedForward,
    ) -> None:
        (
            self._pipelined_modules,
            self._model,
            self._original_forwards,
            self._pipelined_preprocs,
        ) = _rewrite_model(
            model=self._model,
            context=context,
            dist_stream=self._data_dist_stream,
            batch=batch,
            apply_jit=self._apply_jit,
            pipelined_forward=pipelined_forward,
            pipeline_preproc=self._pipeline_preproc,
        )
        # initializes input dist, so we can override input dist forwards
        self.start_sparse_data_dist(batch, context)
        self._original_kjt_dist_forwards = _override_input_dist_forwards(
            self._pipelined_modules
        )

    def _init_pipelined_modules(
        self,
        batch: In,
        context: TrainPipelineContext,
        pipelined_forward: Type[PipelinedForward] = PipelinedForward,
    ) -> None:
        """
        Retrieves the pipelined modules after overriding their forwards, initializes the
        modules' input dists, and overrides the input dist forwards to support fusing
        the splits collective in the input dist.
        """
        if self._pipelined_modules:
            self._set_module_context(context)
            self.start_sparse_data_dist(batch, context)
            return

        self._pipeline_model(batch, context, pipelined_forward)

    def copy_batch_to_gpu(
        self,
        dataloader_iter: Iterator[In],
    ) -> Tuple[Optional[In], Optional[TrainPipelineContext]]:
        """
        Retrieves batch from dataloader and moves it to the provided device.

        Raises:
            StopIteration: if the dataloader iterator is exhausted; unless
                `self._execute_all_batches=True`, then returns None.
        """
        context = None
        with record_function(f"## copy_batch_to_gpu {self._next_index} ##"):
            with self._stream_context(self._memcpy_stream):
                batch = self._next_batch(dataloader_iter)
                if batch is not None:
                    batch = _to_device(batch, self._device, non_blocking=True)
                elif not self._execute_all_batches:
                    raise StopIteration
                context = self._create_context()
                return batch, context

    def _next_batch(self, dataloader_iter: Iterator[In]) -> Optional[In]:
        """
        Retrieves next batch from dataloader and prevents calling `next` on an already
        exhausted dataloader, which can cause hanging.
        """
        if dataloader_iter is not self._dataloader_iter:
            self._dataloader_iter = dataloader_iter
            self._dataloader_exhausted = False

        if self._dataloader_exhausted:
            batch = None
        else:
            with record_function("## next_batch ##"):
                batch = next(dataloader_iter, None)
            if batch is None:
                self._dataloader_exhausted = True
        return batch

    def start_sparse_data_dist(
        self, batch: Optional[In], context: TrainPipelineContext
    ) -> None:
        """
        Waits for batch to finish getting copied to GPU, then starts the input dist.
        """
        if batch is None:
            return
        with record_function(f"## start_sparse_data_dist {context.index} ##"):
            with self._stream_context(self._data_dist_stream):
                _wait_for_batch(batch, self._memcpy_stream)

                original_contexts = [p.get_context() for p in self._pipelined_preprocs]

                # Temporarily set context for next iter to populate cache
                for preproc_mod in self._pipelined_preprocs:
                    preproc_mod.set_context(context)

                _start_data_dist(self._pipelined_modules, batch, context)

                # Restore context for model fwd
                for module, context in zip(self._pipelined_preprocs, original_contexts):
                    module.set_context(context)

    def wait_sparse_data_dist(self, context: TrainPipelineContext) -> None:
        """
        Waits on the input dist splits requests to get the input dist tensors requests,
        and populates the context with them.
        """
        with record_function(f"## wait_sparse_data_dist {context.index} ##"):
            with self._stream_context(self._data_dist_stream):
                for names, awaitable in context.fused_splits_awaitables:
                    for name, request in zip(names, awaitable.wait()):
                        context.input_dist_tensors_requests[name] = request
        context.input_dist_splits_requests.clear()
        context.fused_splits_awaitables.clear()

    def _copy_batch_to_gpu(self, dataloader_iter: Iterator[In]) -> Optional[In]:
        """
        DEPRECATED: exists for backward compatibility on TrainPipelineContext.version 0
        """
        self._set_module_context(self._context)
        batch, _ = self.copy_batch_to_gpu(dataloader_iter)
        return batch

    def _start_sparse_data_dist(self, batch: Optional[In]) -> None:
        """
        DEPRECATED: exists for backward compatibility
        Waits for batch to finish getting copied to GPU, then starts the input dist.
        """
        self._set_module_context(self._context)
        self.start_sparse_data_dist(batch, self._context)

    def _wait_sparse_data_dist(self) -> None:
        """
        DEPRECATED: exists for backward compatibility
        Waits on the input dist splits requests to get the input dist tensors requests,
        and populates the context with them.
        """
        self._set_module_context(self._context)
        with record_function("## wait_sparse_data_dist ##"):
            with self._stream_context(self._data_dist_stream):
                self._context.module_contexts = (
                    self._context.module_contexts_next_batch.copy()
                )
                self._context.input_dist_tensors_requests.clear()
                for names, awaitable in self._context.fused_splits_awaitables:
                    for name, request in zip(names, awaitable.wait()):
                        self._context.input_dist_tensors_requests[name] = request

    def _fill_pipeline(self, dataloader_iter: Iterator[In]) -> None:
        """
        DEPRECATED: exists for backward compatibility
        """
        # pipeline is already filled
        if self._batch_i and self._batch_ip1:
            return
        # executes last batch in pipeline
        if self._batch_i and self._execute_all_batches:
            return

        # batch 1
        self._batch_i = self._copy_batch_to_gpu(dataloader_iter)
        if self._batch_i is None:
            raise StopIteration

        self._init_pipelined_modules(self._batch_i, self._context)
        self._start_sparse_data_dist(self._batch_i)
        self._wait_sparse_data_dist()

        # batch 2
        self._batch_ip1 = self._copy_batch_to_gpu(dataloader_iter)


class TrainPipelineSemiSync(TrainPipelineSparseDist[In, Out]):
    """
    Novel method for RecSys model training by leveraging "Semi-Synchronous" training,
    where the model is still synchronous but each batch prediction is calculated
    on parameters which were last updated B-2, instead of the batch prior (ie. B-1).  This
    allows the Embedding All-to-All from B to be fully overlapped with forward pass of B-1; dramatically
    improving peak training performance.


    Args:
        model (torch.nn.Module): model to pipeline.
        optimizer (torch.optim.Optimizer): optimizer to use.
        device (torch.device): device where device transfer, sparse data dist, and
            forward/backward pass will happen.
        execute_all_batches (bool): executes remaining batches in pipeline after
            exhausting dataloader iterator.
        apply_jit (bool): apply torch.jit.script to non-pipelined (unsharded) modules.
        start_batch (int): batch to begin semi-sync training.  Typically small period of synchronous training reduces early stage NEX.
        stash_gradients (bool): if True, will store gradients for each parameter to insure true "Semi-Sync"
            training.  If False, will update dense optimizer as soon as gradients available (naive "Semi-Sync)
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        device: torch.device,
        execute_all_batches: bool = True,
        apply_jit: bool = False,
        start_batch: int = 900,
        stash_gradients: bool = False,
        pipeline_preproc: bool = False,
        custom_model_fwd: Optional[
            Callable[[Optional[In]], Tuple[torch.Tensor, Out]]
        ] = None,
    ) -> None:
        super().__init__(
            model=model,
            optimizer=optimizer,
            device=device,
            execute_all_batches=execute_all_batches,
            apply_jit=apply_jit,
            context_type=EmbeddingTrainPipelineContext,
            pipeline_preproc=pipeline_preproc,
            custom_model_fwd=custom_model_fwd,
        )
        self._start_batch = start_batch
        self._stash_gradients = stash_gradients

        # use two data streams to support two concurrent batches
        self._embedding_odd_stream: Optional[torch.Stream] = (
            (torch.get_device_module(self._device).Stream(priority=0))
            if device.type in ["cuda", "mtia"]
            else None
        )
        self._embedding_even_stream: Optional[torch.Stream] = (
            (torch.get_device_module(self._device).Stream(priority=0))
            if device.type in ["cuda", "mtia"]
            else None
        )
        self._overarch_stream: Optional[torch.Stream] = (
            (torch.get_device_module(self._device).Stream(priority=-1))
            if device.type in ["cuda", "mtia"]
            else None
        )
        self._embedding_odd_streams: List[Optional[torch.Stream]] = []
        self._embedding_even_streams: List[Optional[torch.Stream]] = []
        self._gradients: Dict[str, torch.Tensor] = {}

    def _grad_swap(self) -> None:
        for name, param in self._model.named_parameters():
            grad = self._gradients.get(name, None)
            if param.grad is not None:
                self._gradients[name] = param.grad.clone()
            param.grad = grad

    def _init_embedding_streams(self) -> None:

        for _ in self._pipelined_modules:
            self._embedding_odd_streams.append(
                (torch.get_device_module(self._device).Stream(priority=0))
                if self._device.type in ["cuda", "mtia"]
                else None
            )
            self._embedding_even_streams.append(
                (torch.get_device_module(self._device).Stream(priority=0))
                if self._device.type in ["cuda", "mtia"]
                else None
            )

    def _validate_optimizer(self) -> None:
        for pipelined_module in self._pipelined_modules:
            pipelined_params = set(pipelined_module.parameters())
            for group in self._optimizer.param_groups:
                if not set(group["params"]).isdisjoint(pipelined_params):
                    logger.warning(
                        f"SemiSync pipelined {type(pipelined_module)} and optimizer share parameters"
                    )

    def fill_pipeline(self, dataloader_iter: Iterator[In]) -> None:
        # pipeline is already filled
        if len(self.batches) >= 3:
            return
        # executes last batch in pipeline
        if self.batches and self._execute_all_batches:
            return

        # batch i
        if not self.enqueue_batch(dataloader_iter):
            return

        self._init_pipelined_modules(
            # pyre-ignore [6]
            self.batches[0],
            self.contexts[0],
            # pyre-ignore [6]
            EmbeddingPipelinedForward,
        )
        self._init_embedding_streams()
        self.wait_sparse_data_dist(self.contexts[0])
        self._validate_optimizer()
        # pyre-ignore [6]
        self.start_embedding_lookup(self.batches[0], self.contexts[0])

        # batch i+1
        if not self.enqueue_batch(dataloader_iter):
            return
        self.start_sparse_data_dist(self.batches[1], self.contexts[1])
        self.wait_sparse_data_dist(self.contexts[1])

        # batch i+2
        if not self.enqueue_batch(dataloader_iter):
            return

    def is_semi_sync(self) -> bool:
        if len(self.batches) >= 1:
            # pyre-ignore [58]
            return self.contexts[0].index >= self._start_batch
        return False

    def _mlp_optimizer_step(self) -> None:
        # special case: not all optimizers support optim.step() on null gradidents
        if (
            len(self.batches) >= 1
            and self.contexts[0].index == self._start_batch
            and self._stash_gradients
        ):
            return
        self._optimizer.step()

    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        self.fill_pipeline(dataloader_iter)
        if not self.batches:
            raise StopIteration

        if len(self.batches) >= 3:
            self.start_sparse_data_dist(
                self.batches[2],
                self.contexts[2],
            )

        losses, output = self._mlp_forward(cast(In, self.batches[0]), self.contexts[0])

        # batch i+3
        self.enqueue_batch(dataloader_iter)

        if len(self.batches) >= 2 and self.is_semi_sync():
            # pyre-ignore [6]
            self.start_embedding_lookup(self.batches[1], self.contexts[1])

        if len(self.batches) >= 3:
            self.wait_sparse_data_dist(self.contexts[2])

        if self._model.training:
            with record_function(f"## backward {self.contexts[0].index} ##"):
                torch.sum(losses, dim=0).backward()
            # pyre-ignore [6]
            self.embedding_backward(self.contexts[0])

            with record_function(
                f"## optimizer {cast(int, self.contexts[0].index) - 1} ##"
            ):
                if self.is_semi_sync() and self._stash_gradients:
                    self._grad_swap()
                self._mlp_optimizer_step()

            with record_function(
                f"## zero_grad {cast(int, self.contexts[0].index) - 1} ##"
            ):
                self._optimizer.zero_grad()

        if len(self.batches) >= 2 and not self.is_semi_sync():
            torch.cuda.synchronize()  # needed to avoid race condition
            # pyre-ignore [6]
            self.start_embedding_lookup(self.batches[1], self.contexts[1])

        self.dequeue_batch()
        return output

    def _mlp_forward(
        self, batch: In, context: TrainPipelineContext
    ) -> Tuple[torch.Tensor, Out]:
        with record_function(f"## forward {context.index} ##"):
            _wait_for_events(
                batch, context, torch.get_device_module(self._device).current_stream()
            )
            return self._model_fwd(batch)

    def embedding_backward(self, context: EmbeddingTrainPipelineContext) -> None:
        default_stream = torch.get_device_module(self._device).current_stream()
        streams = (
            self._embedding_even_streams
            if cast(int, context.index) % 2 == 0
            else self._embedding_odd_streams
        )
        assert len(context.embedding_features) == len(context.embedding_tensors)
        for stream, emb_tensors, embedding_features, detached_emb_tensors in zip(
            streams,
            context.embedding_tensors,
            context.embedding_features,
            context.detached_embedding_tensors,
        ):
            with self._stream_context(stream):
                grads = [tensor.grad for tensor in detached_emb_tensors]
                if stream:
                    stream.wait_stream(default_stream)
                # Some embeddings may never get used in the final loss computation,
                # so the grads will be `None`. If we don't exclude these, it will fail
                # with error: "grad can be implicitly created only for scalar outputs"
                # Alternatively, if the tensor has only 1 element, pytorch can still
                # figure out how to do autograd
                embs_to_backprop, grads_to_use, invalid_features = [], [], []
                assert len(embedding_features) == len(emb_tensors)
                for features, tensor, grad in zip(
                    embedding_features, emb_tensors, grads
                ):
                    if tensor.numel() == 1 or grad is not None:
                        embs_to_backprop.append(tensor)
                        grads_to_use.append(grad)
                    else:
                        if isinstance(features, Iterable):
                            invalid_features.extend(features)
                        else:
                            invalid_features.append(features)
                if invalid_features and context.index == 0:
                    logger.warning(
                        f"SemiSync, the following features have no gradients: {invalid_features}"
                    )
                torch.autograd.backward(embs_to_backprop, grads_to_use)

    def copy_batch_to_gpu(
        self,
        dataloader_iter: Iterator[In],
    ) -> Tuple[Optional[In], Optional[TrainPipelineContext]]:
        context = None
        with record_function(f"## copy_batch_to_gpu {self._next_index} ##"):
            with self._stream_context(self._memcpy_stream):
                batch = self._next_batch(dataloader_iter)
                if batch is not None:
                    batch = _to_device(batch, self._device, non_blocking=True)
                    context = self._create_context()
                    event = torch.get_device_module(self._device).Event()
                    event.record()
                    context.events.append(event)
                return batch, context

    def extract_model_input_from_batch(self, batch: In) -> Pipelineable:
        return batch

    def start_sparse_data_dist(
        self,
        batch: Optional[In],
        context: TrainPipelineContext,
    ) -> None:
        """
        Waits for batch to finish getting copied to GPU, then starts the input dist.  This is Event based version.
        """
        if batch is None:
            return

        # Temporarily set context for next iter to populate cache
        original_contexts = [p.get_context() for p in self._pipelined_preprocs]
        for preproc_mod in self._pipelined_preprocs:
            preproc_mod.set_context(context)

        with record_function(f"## start_sparse_data_dist {context.index} ##"):
            with self._stream_context(self._data_dist_stream):
                _wait_for_events(batch, context, self._data_dist_stream)
                model_input = self.extract_model_input_from_batch(batch)
                _start_data_dist(self._pipelined_modules, model_input, context)
                event = torch.get_device_module(self._device).Event()
                event.record()
                context.events.append(event)

        # Restore context for model forward
        for module, context in zip(self._pipelined_preprocs, original_contexts):
            module.set_context(context)

    def start_embedding_lookup(
        self,
        batch: Optional[In],
        context: EmbeddingTrainPipelineContext,
    ) -> None:
        """
        Waits for batch to finish getting copied to GPU, then starts the input dist. This Event based vesrion.
        """
        if batch is None:
            return
        with record_function(f"## start_embedding_lookup {context.index} ##"):
            _wait_for_events(
                batch, context, torch.get_device_module(self._device).current_stream()
            )
            for i, module in enumerate(self._pipelined_modules):
                stream = (
                    self._embedding_even_streams[i]
                    if cast(int, context.index) % 2 == 0
                    else self._embedding_odd_streams[i]
                )
                with self._stream_context(stream):
                    _start_embedding_lookup(module, context, stream)
                    event = torch.get_device_module(self._device).Event()
                    event.record()
                    context.events.append(event)


class PrefetchTrainPipelineSparseDist(TrainPipelineSparseDist[In, Out]):
    """
    This pipeline overlaps device transfer, `ShardedModule.input_dist()`, and cache
    prefetching with forward and backward. This helps hide the all2all latency while
    preserving the training forward / backward ordering.

    stage 4: forward, backward - uses default CUDA stream
    stage 3: prefetch - uses prefetch CUDA stream
    stage 2: ShardedModule.input_dist() - uses data_dist CUDA stream
    stage 1: device transfer - uses memcpy CUDA stream

    `ShardedModule.input_dist()` is only done for top-level modules in the call graph.
    To be considered a top-level module, a module can only depend on 'getattr' calls on
    input.

    Input model must be symbolically traceable with the exception of `ShardedModule` and
    `DistributedDataParallel` modules.

    Args:
        model (torch.nn.Module): model to pipeline.
        optimizer (torch.optim.Optimizer): optimizer to use.
        device (torch.device): device where device transfer, sparse data dist, prefetch,
            and forward/backward pass will happen.
        execute_all_batches (bool): executes remaining batches in pipeline after
            exhausting dataloader iterator.
        apply_jit (bool): apply torch.jit.script to non-pipelined (unsharded) modules.
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        device: torch.device,
        execute_all_batches: bool = True,
        apply_jit: bool = False,
        pipeline_preproc: bool = False,
        custom_model_fwd: Optional[
            Callable[[Optional[In]], Tuple[torch.Tensor, Out]]
        ] = None,
    ) -> None:
        super().__init__(
            model=model,
            optimizer=optimizer,
            device=device,
            execute_all_batches=execute_all_batches,
            apply_jit=apply_jit,
            context_type=PrefetchTrainPipelineContext,
            pipeline_preproc=pipeline_preproc,
            custom_model_fwd=custom_model_fwd,
        )
        self._context = PrefetchTrainPipelineContext(version=0)
        self._prefetch_stream: Optional[torch.Stream] = (
            (torch.get_device_module(device).Stream())
            if self._device.type in ["cuda", "mtia"]
            else None
        )
        self._default_stream: Optional[torch.Stream] = (
            (torch.get_device_module(self._device).Stream())
            if self._device.type in ["cuda", "mtia"]
            else None
        )
        self._batch_ip3: Optional[In] = None

    def _fill_pipeline(self, dataloader_iter: Iterator[In]) -> None:
        # pipeline is already filled
        if self._batch_i and self._batch_ip1 and self._batch_ip2:
            return
        # executes last batch in pipeline
        if self._execute_all_batches and (self._batch_i or self._batch_ip1):
            return

        # batch 1
        self._batch_i = self._copy_batch_to_gpu(dataloader_iter)
        if self._batch_i is None:
            raise StopIteration

        self._init_pipelined_modules(
            self._batch_i,
            self._context,
            # pyre-ignore
            PrefetchPipelinedForward,
        )
        self._start_sparse_data_dist(self._batch_i)
        self._wait_sparse_data_dist()
        self._prefetch(self._batch_i)

        # batch 2
        self._batch_ip1 = self._copy_batch_to_gpu(dataloader_iter)
        self._start_sparse_data_dist(self._batch_ip1)

    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        self._fill_pipeline(dataloader_iter)

        if self._model.training:
            with record_function("## zero_grad ##"):
                self._optimizer.zero_grad()

        with record_function("## wait_for_batch ##"):
            _wait_for_batch(cast(In, self._batch_i), self._prefetch_stream)

        self._batch_ip2 = self._copy_batch_to_gpu(dataloader_iter)

        self._wait_sparse_data_dist()
        # forward
        with record_function("## forward ##"):
            losses, output = self._model_fwd(self._batch_i)

        self._prefetch(self._batch_ip1)

        if self._model.training:
            # backward
            with record_function("## backward ##"):
                torch.sum(losses, dim=0).backward()

            # update
            with record_function("## optimizer ##"):
                self._optimizer.step()

        self._start_sparse_data_dist(self._batch_ip2)

        self._batch_i = self._batch_ip1
        self._batch_ip1 = self._batch_ip2

        return output

    def _prefetch(self, batch: Optional[In]) -> None:
        """
        Waits for input dist to finish, then prefetches data.
        """
        if batch is None:
            return
        self._context.module_input_post_prefetch.clear()
        self._context.module_contexts_post_prefetch.clear()

        with record_function("## sharded_module_prefetch ##"):
            with self._stream_context(self._prefetch_stream):
                batch.record_stream(
                    torch.get_device_module(self._device).current_stream()
                )
                data_per_pipelined_module = _prefetch_embeddings(
                    batch,
                    self._context,
                    self._pipelined_modules,
                    self._device,
                    self._stream_context,
                    self._data_dist_stream,
                    self._default_stream,
                )
                for sharded_module in self._pipelined_modules:
                    forward = sharded_module.forward
                    data = data_per_pipelined_module[forward._name]
                    self._context.module_input_post_prefetch[forward._name] = data
                    self._context.module_contexts_post_prefetch[forward._name] = (
                        self._context.module_contexts.pop(forward._name)
                    )


class EvalPipelineSparseDist(TrainPipelineSparseDist[In, Out]):
    """
    This pipeline overlaps device transfer, and `ShardedModule.input_dist()` with
    forward. This helps hide the all2all latency. We use a background thread to
    perform device transfer to further reduce latency.

    stage 2: forward- uses default CUDA stream
    stage 1: ShardedModule.input_dist() - uses data_dist CUDA stream
    background: device transfer - uses memcpy CUDA stream

    `ShardedModule.input_dist()` is only done for top-level modules in the call graph.
    To be considered a top-level module, a module can only depend on 'getattr' calls on
    input.

    Input model must be symbolically traceable with the exception of `ShardedModule` and
    `DistributedDataParallel` modules.

    Args:
        model (torch.nn.Module): model to pipeline.
        optimizer (torch.optim.Optimizer): optimizer to use.
        device (torch.device): device where device transfer, sparse data dist, and
            forward/backward pass will happen.
        apply_jit (bool): apply torch.jit.script to non-pipelined (unsharded) modules.
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        device: torch.device,
        apply_jit: bool = False,
    ) -> None:
        super().__init__(model, optimizer, device, True, apply_jit)
        self._batch_loader: Optional[DataLoadingThread[In]] = None

    def __del__(self) -> None:
        if self._batch_loader is not None:
            self._batch_loader.stop()

    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        if not self._batch_loader:
            self._batch_loader = DataLoadingThread(
                device=self._device,
                dataloader_iter=dataloader_iter,
                to_device_non_blocking=True,
                memcpy_stream_priority=-1,
                memcpy_stream=self._memcpy_stream,
            )
            self._batch_loader.start()

            # batch 0
            # pyre-ignore [16]
            batch = self._batch_loader.get_next_batch()
            if batch is None:
                raise StopIteration
            self.batches.append(batch)
            self.contexts.append(self._create_context())

            self._init_pipelined_modules(
                # pyre-ignore
                self.batches[0],
                self.contexts[0],
                PipelinedForward,
            )
            self.start_sparse_data_dist(self.batches[0], self.contexts[0])
            self.wait_sparse_data_dist(self.contexts[0])

        batch = self._batch_loader.get_next_batch()
        if batch is not None:
            self.batches.append(batch)
            self.contexts.append(self._create_context())

        if len(self.batches) == 0:
            raise StopIteration

        with record_function("## wait_for_batch ##"):
            _wait_for_batch(cast(In, self.batches[0]), self._data_dist_stream)

        if len(self.batches) >= 2:
            self.start_sparse_data_dist(self.batches[1], self.contexts[1])

        # forward
        with record_function("## forward ##"):
            losses, output = cast(
                Tuple[torch.Tensor, Out], self._model(self.batches[0])
            )

        if len(self.batches) >= 2:
            self.wait_sparse_data_dist(self.contexts[1])
        self.dequeue_batch()

        return output


class StagedTrainPipeline(TrainPipeline[In, Optional[StageOut]]):
    """
    StagedTrainPipeline orchestrates the pipelined execution of its constituent stages
    from inputs of `dataloader_iter`. Namely scheduling the execution of stages before
    model forward.

    NOTE: the SDD stage needs to be the final stage of the pipeline so that the
        `ShardedModule` forward can properly consume the SDD output.

    Calling progress on a `StagedTrainPipeline` provides an output that is equivalent to
    calling each of the pipeline stages in order.

    In the example below a fully synchronous will expose the `data_copy` and
    `gpu_preproc` calls. After pipelining, the `data_copy` of batch i+2 can be
    overlapped with the `gpu_preproc` of batch i+1 and the main model processing of
    batch i.

    Args:
        pipeline_stages (List[PipelineStage]): A list of stages to execute.
        debug_mode (bool): Whether to enable debug mode.
        compute_stream (Optional[torch.cuda.Stream]): The main compute stream in which
            model forward is run, usually torch.cuda.default_stream(). Defaults to the
            current cuda stream.
        on_flush_end (Optional): Callback function that gets invoked after the pipeline
            has been flushed.

    Example::
        train_pipeline = StagedTrainPipeline(
            pipeline=[
                PipelineStage(
                    name="data_copy",
                    runnable=get_h2d_func("cuda"),
                    stream=torch.cuda.Stream(),
                ),
                PipelineStage(
                    name="gpu_preproc",
                    runnable=gpu_preproc,
                    stream=torch.cuda.Stream(),
                ),
            ]
        )

        while batch_for_forward := train_pipeline.progress(dataloader_iter):
            optimizer.zero_grad()
            loss, pred = model(batch_for_forward)
            loss.backward()
            optimizer.step()
    """

    def __init__(
        self,
        pipeline_stages: List[PipelineStage],
        debug_mode: bool = False,
        compute_stream: Optional[Union[torch.cuda.Stream, torch.mtia.Stream]] = None,
        on_flush_end: Optional[Callable[[], None]] = None,
    ) -> None:
        self._pipeline_stages = pipeline_stages
        self._debug_mode = debug_mode
        self._stage_outputs: List[Optional[StageOutputWithEvent]] = cast(
            List[Optional[StageOutputWithEvent]], [None] * len(self._pipeline_stages)
        )
        self._initialized = False
        self._num_steps = 0
        self._dataloader_iter: Optional[Iterator[In]] = None
        self._dataloader_exhausted: bool = False
        self._compute_stream: torch.Stream = (
            compute_stream
            or torch.get_device_module(
                self._pipeline_stages[0].stream.device
            ).current_stream()
        )

        # pyre-ignore
        self._stream_context = (
            torch.get_device_module(self._compute_stream.device).stream
            if self._compute_stream.device.type in ["cuda", "mtia"]
            else torch.cuda.stream
        )

        self._flushing: bool = False
        self.on_flush_end = on_flush_end

    @property
    def num_stages(self) -> int:
        return len(self._pipeline_stages)

    def _advance(self) -> Optional[StageOutputWithEvent]:
        # left shifts all batch results.
        out = self._stage_outputs[0]
        for idx in range(self.num_stages - 1):
            self._stage_outputs[idx] = self._stage_outputs[idx + 1]
        self._stage_outputs[-1] = None
        return out

    def _run_with_event(
        self,
        runnable: RunnableType,
        event: Optional[torch.Event],
        inputs: Optional[In],
        stream: torch.Stream,
    ) -> StageOutputWithEvent:
        if inputs is None:
            return (None, None)
        with self._stream_context(stream):
            # If there is no previous event, data is entering the pipeline
            if event is not None:
                event.wait(stream)
                inputs.record_stream(stream)

            output = runnable(inputs)
            new_event = torch.get_device_module(stream.device).Event()
            new_event.record(stream)
            return (output, new_event)

    def _next_batch(self, dataloader_iter: Iterator[In]) -> Optional[In]:
        """
        Retrieves next batch from dataloader and prevents calling `next` on an already
        exhausted dataloader, which can cause hanging.
        """
        if dataloader_iter is not self._dataloader_iter:
            self._dataloader_iter = dataloader_iter
            self._dataloader_exhausted = False

        if self._dataloader_exhausted or self._flushing:
            batch = None
        else:
            with record_function("## next_batch ##"):
                batch = next(dataloader_iter, None)
            if batch is None:
                self._dataloader_exhausted = True
        return batch

    def _run_stage(
        self,
        batch_offset: int,
        stage_idx: int,
        dataloader_iter: Iterator[In],
        fill: bool = False,
    ) -> StageOutputWithEvent:
        """
        Each stage of the pipeline MUST have an input and output. If the input is None,
        it means there is no more data to process. The stage will short circuit and NOT
        execute the runnable.
        """
        stage = self._pipeline_stages[stage_idx]

        with record_function(
            f"## Pipeline Stage {stage_idx} : {stage.name} for batch {batch_offset + self._num_steps} ##"
        ):
            if stage_idx == 0:
                batch_to_wait = self._next_batch(dataloader_iter)
                event = None
            else:
                batch_to_wait_with_event = self._stage_outputs[batch_offset]
                assert batch_to_wait_with_event is not None
                batch_to_wait, event = batch_to_wait_with_event

            new_result = self._run_with_event(
                runnable=stage.runnable,
                event=event,
                inputs=batch_to_wait,
                stream=stage.stream,
            )

        self._stage_outputs[batch_offset] = new_result
        if self._debug_mode:
            logger.info(
                f"Running ## Pipeline Stage {stage_idx} : {stage.name} for batch {batch_offset + self._num_steps} ##",
            )

        if fill and (fill_callback := stage.fill_callback) is not None:
            if self._debug_mode:
                logger.info(f"Finished callback for {stage.name}")
            fill_callback()

        return new_result

    def _fill_pipeline(self, dataloader_iter: Iterator[In]) -> None:
        """
        There should always be `self.num_stages` batches in flight. This function
        initializes the pipeline by filling it with `self.num_stages` batches.
        Intuitively, it does all the stages before the model forward.

        NOTE:
            model forward should be executed outside the pipeline in the train loop,
            using the output of `progress` as its input.

        For a 3 stage pipeline during `_fill_pipeline`:
            batch 0: stages 0, 1, 2 will be run
            batch 1: stages 0, 1 will be run
            batch 2: stage 0 will be run
            batch 3: will start in `progress()`

        In the initial `progress()`
            batch 0: model forward will be run
            batch 1: stage 2 will be run
            batch 2: stage 1 will be run
            batch 3: stage 0 will be run
        """
        for batch_offset in range(self.num_stages):
            stages_to_run = self.num_stages - batch_offset
            for stage_idx in range(stages_to_run):
                self._run_stage(
                    batch_offset=batch_offset,
                    stage_idx=stage_idx,
                    dataloader_iter=dataloader_iter,
                    fill=True,
                )

        self._initialized = True
        if self._debug_mode:
            logger.info("Finished fill pipeline")

    def set_flush(self, flush: bool) -> None:
        """
        Sets whether the pipeline should be flushed.

        When the pipeline is in a flushing state, it will stop getting further data from the dataloader and will continue executing the pipeline until all remaining stages are finished. Afterwards, it will invoke a callback and resume the pipeline.
        """
        self._flushing = flush

    def flush_end(self) -> None:
        self.set_flush(False)
        # Ensure pipeline gets filled again
        self._initialized = False

        if self.on_flush_end is not None:
            self.on_flush_end()

    def progress(
        self,
        dataloader_iter: Iterator[In],
    ) -> Optional[StageOut]:
        """
        The pipeline processes data in reverse order, so stage_0 processes the
        newest data and stage_n processes the oldest.

        NOTE:
            if SDD is enabled it must be the last stage in the pipeline.

        Args:
            data_iter (Iterator[In]): An iterator that produces the inputs to
                the pipeline.

        Returns:
            Optional[StageOut]: Output of the final stage. `None` signifies that the
                dataloader iterator is depleted.
        """
        if not self._initialized:
            self._fill_pipeline(dataloader_iter)

        output_with_event = self._advance()

        if output_with_event is None:
            # All data consumed, exit early
            return None

        self._num_steps += 1

        for stage_idx in range(self.num_stages):
            stage_output_idx = self.num_stages - 1 - stage_idx
            self._run_stage(
                batch_offset=stage_output_idx,
                stage_idx=stage_idx,
                dataloader_iter=dataloader_iter,
            )

        out, event = output_with_event
        if event is not None:
            # Since model forward() is expected to run outside the pipeline,
            # we need to explicitly wait for the last stage to finish
            event.wait(self._compute_stream)
            out.record_stream(self._compute_stream)

        if out is None and self._flushing:
            # We have exhausted all stages due to flushing
            self.flush_end()
            return self.progress(dataloader_iter)

        return out


class TrainPipelineSparseDistCompAutograd(TrainPipelineSparseDist[In, Out]):
    """
    This pipeline clone the TrainPipelineSparseDist, but execute the progress
    method within compiled autograd context.
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        device: torch.device,
        execute_all_batches: bool = True,
        apply_jit: bool = False,
        context_type: Type[TrainPipelineContext] = TrainPipelineContext,
        pipeline_preproc: bool = False,
        custom_model_fwd: Optional[
            Callable[[Optional[In]], Tuple[torch.Tensor, Out]]
        ] = None,
    ) -> None:
        super().__init__(
            model,
            optimizer,
            device,
            execute_all_batches,
            apply_jit,
            context_type,
            pipeline_preproc,
            custom_model_fwd,
        )

        torch._logging.set_logs(compiled_autograd_verbose=True)

        # it will check this path on model to inject configuration other than
        # the default one.
        # pyre-fixme[8]: Attribute has type `Dict[str, Union[bool, str]]`; used as
        #  `Union[Tensor, Module]`.
        self.compiled_autograd_options: Dict[str, Union[str, bool]] = getattr(
            model,
            "_compiled_autograd_options",
            {
                "backend": "inductor",
                "dynamic": True,
                "fullgraph": True,
            },
        )
        torch._dynamo.config.inline_inbuilt_nn_modules = True
        torch._dynamo.config.skip_fsdp_hooks = False
        torch._functorch.config.recompute_views = True
        torch._functorch.config.cse = False
        torch._inductor.config.reorder_for_compute_comm_overlap = True
        torch._inductor.config.reorder_for_compute_comm_overlap_passes = [
            "sink_waits",
            "raise_comms",
            "reorder_compute_for_overlap",
        ]
        self.initialized = False

    def get_compiled_autograd_ctx(
        self,
        # pyre-fixme[24]: Generic type `ContextManager` expects 1 type parameter.
    ) -> ContextManager:
        # this allows for pipelining
        # to avoid doing a sum on None
        # when the pipeline is empty
        if not self.initialized:
            self.initialized = True
            return contextlib.nullcontext()

        return torch._dynamo.compiled_autograd._enable(
            # pyre-ignore
            torch.compile(**self.compiled_autograd_options)
        )

    def progress(self, dataloader_iter: Iterator[In]) -> Out:
        if not self._model_attached:
            self.attach(self._model)

        self.fill_pipeline(dataloader_iter)
        if not self.batches:
            raise StopIteration

        # TODO: Remove once Bulk Eval migrated (needed for bwd compat, this class only)
        self._set_module_context(self.contexts[0])

        if self._model.training:
            with record_function("## zero_grad ##"):
                self._optimizer.zero_grad()

        with record_function("## wait_for_batch ##"):
            _wait_for_batch(cast(In, self.batches[0]), self._data_dist_stream)

        if len(self.batches) >= 2:
            self.start_sparse_data_dist(self.batches[1], self.contexts[1])

        # batch i+2
        self.enqueue_batch(dataloader_iter)

        # forward
        ctx = self.get_compiled_autograd_ctx()
        with ctx, torchrec_use_sync_collectives(), record_function("## forward ##"):
            losses, output = self._model_fwd(self.batches[0])

        if len(self.batches) >= 2:
            self.wait_sparse_data_dist(self.contexts[1])

        if self._model.training:
            # backward
            ctx = self.get_compiled_autograd_ctx()
            with ctx, torchrec_use_sync_collectives(), record_function(
                "## backward ##"
            ):
                torch.sum(losses, dim=0).backward()

            # update
            with record_function("## optimizer ##"):
                self._optimizer.step()

        self.dequeue_batch()
        return output
