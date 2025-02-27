"""
Copyright 2019 The Microsoft DeepSpeed Team
"""

from numpy.core.numeric import count_nonzero
from deepspeed.elasticity.elasticity import compute_elastic_config
import time
import torch
from numpy import mean
from deepspeed.utils.logging import log_dist
from deepspeed import comm as dist

from deepspeed.utils import logger

try:
    import psutil

    PSUTILS_INSTALLED = True
except ImportError:
    PSUTILS_INSTALLED = False
    pass


class CudaEventTimer(object):
    def __init__(self, start_event: torch.cuda.Event, end_event: torch.cuda.Event):
        self.start_event = start_event
        self.end_event = end_event

    def get_elapsed_msec(self):
        torch.cuda.current_stream().wait_event(self.end_event)
        self.end_event.synchronize()
        return self.start_event.elapsed_time(self.end_event)


class SynchronizedWallClockTimer:
    """Group of timers. Borrowed from Nvidia Megatron code"""
    class Timer:
        """Timer."""
        def __init__(self, name):
            self.name_ = name
            self.started_ = False
            self.event_timers = []
            self.start_event = None
            self.elapsed_records = None

        def start(self):
            """Start the timer."""
            assert not self.started_, f"{self.name_} timer has already been started"
            self.start_event = torch.cuda.Event(enable_timing=True)
            self.start_event.record()
            self.started_ = True

        def stop(self, reset=False, record=False):
            """Stop the timer."""
            assert self.started_, "timer is not started"
            end_event = torch.cuda.Event(enable_timing=True)
            end_event.record()
            self.event_timers.append(CudaEventTimer(self.start_event, end_event))
            self.start_event = None
            self.started_ = False

        def _get_elapsed_msec(self):
            self.elapsed_records = [et.get_elapsed_msec() for et in self.event_timers]
            self.event_timers.clear()
            return sum(self.elapsed_records)

        def reset(self):
            """Reset timer."""
            self.started_ = False
            self.start_event = None
            self.elapsed_records = None
            self.event_timers.clear()

        def elapsed(self, reset=True):
            """Calculate the elapsed time."""
            started_ = self.started_
            # If the timing in progress, end it first.
            if self.started_:
                self.stop()
            # Get the elapsed time.
            elapsed_ = self._get_elapsed_msec()
            # Reset the elapsed time
            if reset:
                self.reset()
            # If timing was in progress, set it back.
            if started_:
                self.start()
            return elapsed_

        def mean(self):
            return trim_mean(self.elapsed_records, 0.1)

    def __init__(self):
        self.timers = {}

    def __call__(self, name):
        if name not in self.timers:
            self.timers[name] = self.Timer(name)
        return self.timers[name]

    @staticmethod
    def memory_usage():
        alloc = "mem_allocated: {:.4f} GB".format(torch.cuda.memory_allocated() /
                                                  (1024 * 1024 * 1024))
        max_alloc = "max_mem_allocated: {:.4f} GB".format(
            torch.cuda.max_memory_allocated() / (1024 * 1024 * 1024))
        cache = "cache_allocated: {:.4f} GB".format(torch.cuda.memory_cached() /
                                                    (1024 * 1024 * 1024))
        max_cache = "max_cache_allocated: {:.4f} GB".format(
            torch.cuda.max_memory_cached() / (1024 * 1024 * 1024))
        return " | {} | {} | {} | {}".format(alloc, max_alloc, cache, max_cache)

    def log(self, names, normalizer=1.0, reset=True, memory_breakdown=False, ranks=None):
        """Log a group of timers."""
        assert normalizer > 0.0
        string = f"rank={dist.get_rank()} time (ms)"
        for name in names:
            if name in self.timers:
                elapsed_time = (self.timers[name].elapsed(reset=reset) / normalizer)
                string += " | {}: {:.2f}".format(name, elapsed_time)

        log_dist(string, ranks=ranks or [0])

    def get_mean(self, names, normalizer=1.0, reset=True):
        """Get the mean of a group of timers."""
        assert normalizer > 0.0
        means = {}
        for name in names:
            if name in self.timers:
                elapsed_time = (self.timers[name].mean() * 1000.0 / normalizer)
                means[name] = elapsed_time
        return means


class ThroughputTimer:
    def __init__(
        self,
        batch_size,
        num_workers,
        start_step=2,
        steps_per_output=50,
        monitor_memory=False,
        logging_fn=None,
    ):
        self.start_time = 0
        self.end_time = 0
        self.started = False
        self.batch_size = batch_size
        if batch_size is None:
            self.batch_size = 1
        self.num_workers = num_workers
        self.start_step = start_step
        self.epoch_count = 0
        self.local_step_count = 0
        self.total_step_count = 0
        self.total_elapsed_time = 0
        self.steps_per_output = steps_per_output
        self.monitor_memory = monitor_memory
        self.logging = logging_fn
        if self.logging is None:
            self.logging = logger.info
        self.initialized = False

        if self.monitor_memory and not PSUTILS_INSTALLED:
            raise ImportError("Unable to import 'psutils', please install package")

    def update_epoch_count(self):
        self.epoch_count += 1
        self.local_step_count = 0

    def _init_timer(self):
        self.initialized = True

    def start(self):
        self._init_timer()
        self.started = True
        if self.total_step_count >= self.start_step:
            torch.cuda.synchronize()
            self.start_time = time.time()

    def stop(self, report_speed=True):
        if not self.started:
            return
        self.started = False
        self.total_step_count += 1
        self.local_step_count += 1
        if self.total_step_count > self.start_step:
            torch.cuda.synchronize()
            self.end_time = time.time()
            duration = self.end_time - self.start_time
            self.total_elapsed_time += duration
            if self.local_step_count % self.steps_per_output == 0:
                if report_speed:
                    self.logging(
                        "{}/{}, SamplesPerSec={}, MemAllocated={}GB, MaxMemAllocated={}GB"
                        .format(self.epoch_count,
                                self.local_step_count,
                                self.avg_samples_per_sec(),
                                round(torch.cuda.memory_allocated() / 1024**3,
                                      2),
                                round(torch.cuda.max_memory_allocated() / 1024**3,
                                      2)))
                if self.monitor_memory:
                    virt_mem = psutil.virtual_memory()
                    swap = psutil.swap_memory()
                    self.logging("{}/{}, vm percent: {}, swap percent: {}".format(
                        self.epoch_count,
                        self.local_step_count,
                        virt_mem.percent,
                        swap.percent,
                    ))

    def avg_samples_per_sec(self):
        if self.total_step_count > 0:
            samples_per_step = self.batch_size * self.num_workers
            total_step_offset = self.total_step_count - self.start_step
            avg_time_per_step = self.total_elapsed_time / total_step_offset
            # training samples per second
            return samples_per_step / avg_time_per_step
        return float("-inf")


def trim_mean(data, trim_percent):
    """Compute the trimmed mean of a list of numbers.

    Args:
        data (list): List of numbers.
        trim_percent (float): Percentage of data to trim.

    Returns:
        float: Trimmed mean.
    """
    assert trim_percent >= 0.0 and trim_percent <= 1.0
    n = len(data)
    data.sort()
    k = int(round(n * (trim_percent)))
    return mean(data[k:n - k])
