from tornado import gen
from tornado.iostream import StreamClosedError
import h11

from microproxy.protocol.http1 import Connection
from microproxy.exception import SrcStreamClosedError, DestStreamClosedError
from microproxy.utils import get_logger
logger = get_logger(__name__)


class Http1Layer(object):
    def __init__(self, context):
        super(Http1Layer, self).__init__()
        self.context = context
        self.src_conn = Connection(
            h11.SERVER,
            self.context.src_stream,
            conn_type="src",
            readonly=context.config["mode"] == "replay",
            on_request=self.on_request)
        self.dest_conn = Connection(
            h11.CLIENT,
            self.context.dest_stream,
            conn_type="dest",
            on_response=self.on_response,
            on_info_response=self.on_info_response)
        self.req = None
        self.resp = None
        self.switch_protocol = False

    @gen.coroutine
    def process_and_return_context(self):
        while not self.finished():
            self.req = None
            self.resp = None
            try:
                yield self.run_request()
                yield self.run_response()
            except SrcStreamClosedError:
                self.context.dest_stream.close()
                if self.req:
                    raise
            except DestStreamClosedError:
                self.context.src_stream.close()
                raise

        if self.switch_protocol:
            self.context.scheme = self.req.headers["Upgrade"]
        raise gen.Return(self.context)

    @gen.coroutine
    def run_request(self):
        # NOTE: run first request to handle protocol change
        while not self.req:
            try:
                data = yield self.context.src_stream.read_bytes(
                    self.context.src_stream.max_buffer_size, partial=True)
            except StreamClosedError:
                raise SrcStreamClosedError
            else:
                self.src_conn.receive(data, raise_exception=True)

    @gen.coroutine
    def run_response(self):
        while not self.resp:
            try:
                data = yield self.context.dest_stream.read_bytes(
                    self.context.dest_stream.max_buffer_size, partial=True)
            except StreamClosedError:
                raise DestStreamClosedError
            else:
                self.dest_conn.receive(data, raise_exception=True)

    def on_request(self, request):
        plugin_result = self.context.interceptor.request(
            layer_context=self.context, request=request)

        self.req = plugin_result.request if plugin_result else request
        try:
            self.dest_conn.send_request(self.req)
        except StreamClosedError:
            raise DestStreamClosedError

    def on_response(self, response):
        plugin_result = self.context.interceptor.response(
            layer_context=self.context,
            request=self.req, response=response)

        self.resp = plugin_result.response if plugin_result else response
        try:
            self.src_conn.send_response(self.resp)
        except StreamClosedError:
            raise SrcStreamClosedError

        self.finish()

    def on_info_response(self, response):
        plugin_result = self.context.interceptor.response(
            layer_context=self.context,
            request=self.req, response=response)
        self.resp = plugin_result.response if plugin_result else response
        self.src_conn.send_info_response(self.resp)
        self.finish(switch_protocol=True)

    def finished(self):
        return (self.switch_protocol or
                self.context.src_stream.closed() or
                self.context.dest_stream.closed())

    def finish(self, switch_protocol=False):
        self.context.interceptor.publish(
            layer_context=self.context,
            request=self.req, response=self.resp)
        if self.context.config["mode"] == "replay":
            self.context.src_stream.close()
            self.context.dest_stream.close()
        elif switch_protocol:
            self.switch_protocol = True
        elif self.src_conn.closed() or self.dest_conn.closed():
            self.context.src_stream.close()
            self.context.dest_stream.close()
        else:
            self.src_conn.start_next_cycle()
            self.dest_conn.start_next_cycle()
