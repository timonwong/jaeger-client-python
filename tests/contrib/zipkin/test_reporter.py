# Copyright (c) 2016 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import time

import mock
import pytest
import tornado.gen
from tornado.ioloop import IOLoop
from tornado.testing import AsyncTestCase, gen_test

from jaeger_client import Span, SpanContext
from jaeger_client.contrib.zipkin.reporter import ZipkinReporter
from jaeger_client.ioloop_util import future_result
from jaeger_client.metrics import Metrics
from jaeger_client.utils import ErrorReporter
from tests.test_reporter import (FakeMetricsFactory, FakeSender, FakeTrace,
                                 HardErrorReporter)


class ReporterTest(AsyncTestCase):
    @pytest.yield_fixture
    def thread_loop(self):
        yield

    def _new_span(self, name):
        tracer = FakeTrace(ip_address='127.0.0.1',
                           service_name='reporter_test')
        ctx = SpanContext(trace_id=1, span_id=1, parent_id=None, flags=1)
        span = Span(context=ctx, tracer=tracer, operation_name=name)
        span.start_time = time.time()
        span.end_time = span.start_time + 0.001  # 1ms
        return span

    def _new_reporter(self, batch_size, flush=None, queue_cap=100):
        reporter = ZipkinReporter(transport_handler=mock.MagicMock(),
                                  io_loop=IOLoop.current(),
                                  batch_size=batch_size,
                                  flush_interval=flush,
                                  metrics_factory=FakeMetricsFactory(),
                                  error_reporter=HardErrorReporter(),
                                  queue_capacity=queue_cap)
        sender = FakeSender()
        reporter._send = sender
        return reporter, sender

    @tornado.gen.coroutine
    def _wait_for(self, fn):
        """Wait until fn() returns truth, but not longer than 1 second."""
        start = time.time()
        for i in range(1000):
            if fn():
                return
            yield tornado.gen.sleep(0.001)
        print('waited for condition %f' % (time.time() - start))

    @gen_test
    def test_submit_batch_size_1(self):
        reporter, sender = self._new_reporter(batch_size=1)
        reporter.report_span(self._new_span('1'))

        yield self._wait_for(lambda: len(sender.futures) > 0)
        assert 1 == len(sender.futures)

        sender.futures[0].set_result(1)
        yield reporter.close()
        assert 1 == len(sender.futures)

        # send after close
        span_dropped_key = 'jaeger.spans.dropped_true'
        assert span_dropped_key not in reporter.metrics_factory.counters
        reporter.report_span(self._new_span('1'))
        assert 1 == reporter.metrics_factory.counters[span_dropped_key]

    @gen_test
    def test_submit_failure(self):
        reporter, sender = self._new_reporter(batch_size=1)
        reporter.error_reporter = ErrorReporter(
            metrics=Metrics(), logger=logging.getLogger())

        reporter_failure_key = 'jaeger.spans.reported_false'
        assert reporter_failure_key not in reporter.metrics_factory.counters

        # simulate exception in send
        reporter._send = mock.MagicMock(side_effect=ValueError())
        reporter.report_span(self._new_span('1'))

        yield self._wait_for(
            lambda: reporter_failure_key in reporter.metrics_factory.counters)
        assert 1 == reporter.metrics_factory.counters.get(reporter_failure_key)

        # silly test, for code coverage only
        yield reporter._submit([])

    @gen_test
    def test_submit_queue_full_batch_size_1(self):
        reporter, sender = self._new_reporter(batch_size=1, queue_cap=1)
        reporter.report_span(self._new_span('1'))

        yield self._wait_for(lambda: len(sender.futures) > 0)
        assert 1 == len(sender.futures)
        # the consumer is blocked on a future, so won't drain the queue
        reporter.report_span(self._new_span('2'))
        span_dropped_key = 'jaeger.spans.dropped_true'
        assert span_dropped_key not in reporter.metrics_factory.counters
        reporter.report_span(self._new_span('3'))
        yield self._wait_for(
            lambda: span_dropped_key in reporter.metrics_factory.counters
        )
        assert 1 == reporter.metrics_factory.counters.get(span_dropped_key)
        # let it drain the queue
        sender.futures[0].set_result(1)
        yield self._wait_for(lambda: len(sender.futures) > 1)
        assert 2 == len(sender.futures)

        sender.futures[1].set_result(1)
        yield reporter.close()

    @gen_test
    def test_submit_batch_size_2(self):
        reporter, sender = self._new_reporter(batch_size=2, flush=0.005)
        reporter.report_span(self._new_span('1'))
        yield tornado.gen.sleep(0.001)
        assert 0 == len(sender.futures)

        reporter.report_span(self._new_span('2'))
        yield self._wait_for(lambda: len(sender.futures) > 0)
        assert 1 == len(sender.futures)
        assert 2 == len(sender.requests[0])
        sender.futures[0].set_result(1)

        # 3rd span will not be submitted right away, but after `flush` interval
        reporter.report_span(self._new_span('3'))
        yield tornado.gen.sleep(0.001)
        assert 1 == len(sender.futures)
        yield tornado.gen.sleep(0.001)
        assert 1 == len(sender.futures)
        yield tornado.gen.sleep(0.005)
        assert 2 == len(sender.futures)
        sender.futures[1].set_result(1)

        yield reporter.close()


    @gen_test
    def test_close_drains_queue(self):
        reporter, sender = self._new_reporter(batch_size=1, flush=0.050)
        reporter.report_span(self._new_span('0'))

        yield self._wait_for(lambda: len(sender.futures) > 0)
        assert 1 == len(sender.futures)

        # now that the consumer is blocked on the first future.
        # let's reset Send to actually respond right away
        # and flood the queue with messages
        count = [0]

        def send(_):
            count[0] += 1
            return future_result(True)

        reporter._send = send
        reporter.batch_size = 3
        for i in range(10):
            reporter.report_span(self._new_span('%s' % i))
        assert reporter.queue.qsize() == 10, 'queued 10 spans'

        # now unblock consumer
        sender.futures[0].set_result(1)
        yield self._wait_for(lambda: count[0] > 2)

        assert count[0] == 3, '9 out of 10 spans submitted in 3 batches'
        assert reporter.queue._unfinished_tasks == 1, 'one span still pending'

        yield reporter.close()
        assert reporter.queue.qsize() == 0, 'all spans drained'
        assert count[0] == 4, 'last span submitted in one extrac batch'