from __future__ import absolute_import, division, print_function, with_statement
from tornado.concurrent import Future
from tornado import gen
from tornado import netutil
from tornado.stack_context import NullContext
from tornado.testing import AsyncTestCase, bind_unused_port, gen_test
from tornado.test.util import unittest, skipIfNonUnix, refusing_port

from microproxy.tornado_ext.iostream import MicroProxyIOStream
from microproxy.tornado_ext.iostream import MicroProxySSLIOStream
from microproxy.protocol.tls import create_src_sslcontext
from microproxy.protocol.tls import create_basic_sslcontext
from OpenSSL import crypto
from OpenSSL import SSL
from service_identity import VerificationError

import errno
import os
import platform
import socket
import sys

try:
    from unittest import mock  # type: ignore
except ImportError:
    import mock  # type: ignore


def _server_ssl_options():
    cert_file = "microproxy/test/test.crt"
    private_key_file = "microproxy/test/test.key"

    with open(cert_file, "rb") as fp:
        _buffer = fp.read()
    ca_root = crypto.load_certificate(crypto.FILETYPE_PEM, _buffer)

    with open(private_key_file, "rb") as fp:
        _buffer = fp.read()
    private_key = crypto.load_privatekey(crypto.FILETYPE_PEM, _buffer)
    return create_src_sslcontext(cert=ca_root, priv_key=private_key)


def _client_ssl_options(verify_mode, verify_cb, alpn=None):
    ssl_ctx = create_basic_sslcontext()

    ssl_ctx.set_verify(verify_mode, verify_cb)
    try:
        ssl_ctx.set_alpn_protos(alpn or [])
    except NotImplementedError:
        pass

    return ssl_ctx


class TestIOStreamMixin(object):
    def _make_server_iostream(self, connection, **kwargs):
        raise NotImplementedError()

    def _make_client_iostream(self, connection, **kwargs):
        raise NotImplementedError()

    def make_iostream_pair(self, **kwargs):
        listener, port = bind_unused_port()
        streams = [None, None]

        def accept_callback(connection, address):
            streams[0] = self._make_server_iostream(connection, **kwargs)
            self.stop()

        def connect_callback():
            streams[1] = client_stream
            self.stop()
        netutil.add_accept_handler(listener, accept_callback,
                                   io_loop=self.io_loop)
        client_stream = self._make_client_iostream(socket.socket(), **kwargs)
        client_stream.connect(('127.0.0.1', port),
                              callback=connect_callback)
        self.wait(condition=lambda: all(streams))
        self.io_loop.remove_handler(listener.fileno())
        listener.close()
        return streams

    def test_streaming_callback_with_data_in_buffer(self):
        server, client = self.make_iostream_pair()
        client.write(b"abcd\r\nefgh")
        server.read_until(b"\r\n", self.stop)
        data = self.wait()
        self.assertEqual(data, b"abcd\r\n")

        def closed_callback(chunk):
            self.fail()
        server.read_until_close(callback=closed_callback,
                                streaming_callback=self.stop)
        # self.io_loop.add_timeout(self.io_loop.time() + 0.01, self.stop)
        data = self.wait()
        self.assertEqual(data, b"efgh")
        server.close()
        client.close()

    def test_write_zero_bytes(self):
        # Attempting to write zero bytes should run the callback without
        # going into an infinite loop.
        server, client = self.make_iostream_pair()
        server.write(b'', callback=self.stop)
        self.wait()
        server.close()
        client.close()

    def test_connection_refused(self):
        # When a connection is refused, the connect callback should not
        # be run.  (The kqueue IOLoop used to behave differently from the
        # epoll IOLoop in this respect)
        cleanup_func, port = refusing_port()
        self.addCleanup(cleanup_func)
        stream = MicroProxyIOStream(socket.socket(), io_loop=self.io_loop)
        self.connect_called = False

        def connect_callback():
            self.connect_called = True
            self.stop()
        stream.set_close_callback(self.stop)
        stream.connect(("127.0.0.1", port), connect_callback)
        self.wait()
        self.assertFalse(self.connect_called)
        self.assertTrue(isinstance(stream.error, socket.error), stream.error)
        if sys.platform != 'cygwin':
            _ERRNO_CONNREFUSED = (errno.ECONNREFUSED,)
            if hasattr(errno, "WSAECONNREFUSED"):
                _ERRNO_CONNREFUSED += (errno.WSAECONNREFUSED,)
            # cygwin's errnos don't match those used on native windows python
            self.assertTrue(stream.error.args[0] in _ERRNO_CONNREFUSED)

    @unittest.skipIf(mock is None, 'mock package not present')
    def test_gaierror(self):
        # Test that MicroProxyIOStream sets its exc_info on getaddrinfo error.
        # It's difficult to reliably trigger a getaddrinfo error;
        # some resolvers own't even return errors for malformed names,
        # so we mock it instead. If MicroProxyIOStream changes to call a Resolver
        # before sock.connect, the mock target will need to change too.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        stream = MicroProxyIOStream(s, io_loop=self.io_loop)
        stream.set_close_callback(self.stop)
        with mock.patch('socket.socket.connect',
                        side_effect=socket.gaierror(errno.EIO, 'boom')):
            stream.connect(('localhost', 80), callback=self.stop)
            self.wait()
            self.assertIsInstance(stream.error, socket.gaierror)

    def test_read_callback_error(self):
        # Test that MicroProxyIOStream sets its exc_info when a read callback throws
        server, client = self.make_iostream_pair()
        try:
            server.set_close_callback(self.stop)
            # with ExpectLog(
            #     app_log, "(Uncaught exception|Exception in callback)"
            # ):
            # Clear ExceptionStackContext so MicroProxyIOStream catches error
            with NullContext():
                server.read_bytes(1, callback=lambda data: 1 / 0)
            client.write(b"1")
            self.wait()
            self.assertTrue(isinstance(server.error, ZeroDivisionError))
        finally:
            server.close()
            client.close()

    def test_streaming_callback(self):
        server, client = self.make_iostream_pair()
        try:
            chunks = []
            final_called = []

            def streaming_callback(data):
                chunks.append(data)
                self.stop()

            def final_callback(data):
                self.assertFalse(data)
                final_called.append(True)
                self.stop()
            server.read_bytes(6, callback=final_callback,
                              streaming_callback=streaming_callback)
            client.write(b"1234")
            self.wait(condition=lambda: chunks)
            client.write(b"5678")
            self.wait(condition=lambda: final_called)
            self.assertEqual(chunks, [b"1234", b"56"])

            # the rest of the last chunk is still in the buffer
            server.read_bytes(2, callback=self.stop)
            data = self.wait()
            self.assertEqual(data, b"78")
        finally:
            server.close()
            client.close()

    def test_streaming_until_close(self):
        server, client = self.make_iostream_pair()
        try:
            chunks = []
            closed = [False]

            def streaming_callback(data):
                chunks.append(data)
                self.stop()

            def close_callback(data):
                assert not data, data
                closed[0] = True
                self.stop()
            client.read_until_close(callback=close_callback,
                                    streaming_callback=streaming_callback)
            server.write(b"1234")
            self.wait(condition=lambda: len(chunks) == 1)
            server.write(b"5678", self.stop)
            self.wait()
            server.close()
            self.wait(condition=lambda: closed[0])
            self.assertEqual(chunks, [b"1234", b"5678"])
        finally:
            server.close()
            client.close()

    def test_streaming_until_close_future(self):
        server, client = self.make_iostream_pair()
        try:
            chunks = []

            @gen.coroutine
            def client_task():
                yield client.read_until_close(streaming_callback=chunks.append)

            @gen.coroutine
            def server_task():
                yield server.write(b"1234")
                yield gen.sleep(0.01)
                yield server.write(b"5678")
                server.close()

            @gen.coroutine
            def f():
                yield [client_task(), server_task()]
            self.io_loop.run_sync(f)
            self.assertEqual(chunks, [b"1234", b"5678"])
        finally:
            server.close()
            client.close()

    def test_delayed_close_callback(self):
        # The scenario:  Server closes the connection while there is a pending
        # read that can be served out of buffered data.  The client does not
        # run the close_callback as soon as it detects the close, but rather
        # defers it until after the buffered read has finished.
        server, client = self.make_iostream_pair()
        try:
            client.set_close_callback(self.stop)
            server.write(b"12")
            chunks = []

            def callback1(data):
                chunks.append(data)
                client.read_bytes(1, callback2)
                server.close()

            def callback2(data):
                chunks.append(data)
            client.read_bytes(1, callback1)
            self.wait()  # stopped by close_callback
            self.assertEqual(chunks, [b"1", b"2"])
        finally:
            server.close()
            client.close()

    def test_future_delayed_close_callback(self):
        # Same as test_delayed_close_callback, but with the future interface.
        server, client = self.make_iostream_pair()

        # We can't call make_iostream_pair inside a gen_test function
        # because the ioloop is not reentrant.
        @gen_test
        def f(self):
            server.write(b"12")
            chunks = []
            chunks.append((yield client.read_bytes(1)))
            server.close()
            chunks.append((yield client.read_bytes(1)))
            self.assertEqual(chunks, [b"1", b"2"])
        try:
            f(self)
        finally:
            server.close()
            client.close()

    def test_close_buffered_data(self):
        # Similar to the previous test, but with data stored in the OS's
        # socket buffers instead of the MicroProxyIOStream's read buffer.  Out-of-band
        # close notifications must be delayed until all data has been
        # drained into the MicroProxyIOStream buffer. (epoll used to use out-of-band
        # close events with EPOLLRDHUP, but no longer)
        #
        # This depends on the read_chunk_size being smaller than the
        # OS socket buffer, so make it small.
        server, client = self.make_iostream_pair(read_chunk_size=256)
        try:
            server.write(b"A" * 512)
            client.read_bytes(256, self.stop)
            data = self.wait()
            self.assertEqual(b"A" * 256, data)
            server.close()
            # Allow the close to propagate to the client side of the
            # connection.  Using add_callback instead of add_timeout
            # doesn't seem to work, even with multiple iterations
            self.io_loop.add_timeout(self.io_loop.time() + 0.01, self.stop)
            self.wait()
            client.read_bytes(256, self.stop)
            data = self.wait()
            self.assertEqual(b"A" * 256, data)
        finally:
            server.close()
            client.close()

    def test_read_until_close_after_close(self):
        # Similar to test_delayed_close_callback, but read_until_close takes
        # a separate code path so test it separately.
        server, client = self.make_iostream_pair()
        try:
            server.write(b"1234")
            server.close()
            # Read one byte to make sure the client has received the data.
            # It won't run the close callback as long as there is more buffered
            # data that could satisfy a later read.
            client.read_bytes(1, self.stop)
            data = self.wait()
            self.assertEqual(data, b"1")
            client.read_until_close(self.stop)
            data = self.wait()
            self.assertEqual(data, b"234")
        finally:
            server.close()
            client.close()

    @unittest.skipIf(mock is None, 'mock package not present')
    def test_read_until_close_with_error(self):
        server, client = self.make_iostream_pair()
        try:
            with mock.patch('tornado.iostream.BaseIOStream._try_inline_read',
                            side_effect=IOError('boom')):
                with self.assertRaisesRegexp(IOError, 'boom'):
                    client.read_until_close(self.stop)
        finally:
            server.close()
            client.close()

    def test_streaming_read_until_close_after_close(self):
        # Same as the preceding test but with a streaming_callback.
        # All data should go through the streaming callback,
        # and the final read callback just gets an empty string.
        server, client = self.make_iostream_pair()
        try:
            server.write(b"1234")
            server.close()
            client.read_bytes(1, self.stop)
            data = self.wait()
            self.assertEqual(data, b"1")
            streaming_data = []
            client.read_until_close(self.stop,
                                    streaming_callback=streaming_data.append)
            data = self.wait()
            self.assertEqual(b'', data)
            self.assertEqual(b''.join(streaming_data), b"234")
        finally:
            server.close()
            client.close()

    def test_large_read_until(self):
        # Performance test: read_until used to have a quadratic component
        # so a read_until of 4MB would take 8 seconds; now it takes 0.25
        # seconds.
        server, client = self.make_iostream_pair()
        try:
            # This test fails on pypy with ssl.  I think it's because
            # pypy's gc defeats moves objects, breaking the
            # "frozen write buffer" assumption.
            if (isinstance(server, MicroProxySSLIOStream) and
                    platform.python_implementation() == 'PyPy'):
                raise unittest.SkipTest(
                    "pypy gc causes problems with openssl")
            NUM_KB = 4096
            for i in range(NUM_KB):
                client.write(b"A" * 1024)
            client.write(b"\r\n")
            server.read_until(b"\r\n", self.stop)
            data = self.wait()
            self.assertEqual(len(data), NUM_KB * 1024 + 2)
        finally:
            server.close()
            client.close()

    def test_close_callback_with_pending_read(self):
        # Regression test for a bug that was introduced in 2.3
        # where the MicroProxyIOStream._close_callback would never be called
        # if there were pending reads.
        OK = b"OK\r\n"
        server, client = self.make_iostream_pair()
        client.set_close_callback(self.stop)
        try:
            server.write(OK)
            client.read_until(b"\r\n", self.stop)
            res = self.wait()
            self.assertEqual(res, OK)

            server.close()
            client.read_until(b"\r\n", lambda x: x)
            # If _close_callback (self.stop) is not called,
            # an AssertionError: Async operation timed out after 5 seconds
            # will be raised.
            res = self.wait()
            self.assertTrue(res is None)
        finally:
            server.close()
            client.close()

    @skipIfNonUnix
    def test_inline_read_error(self):
        # An error on an inline read is raised without logging (on the
        # assumption that it will eventually be noticed or logged further
        # up the stack).
        #
        # This test is posix-only because windows os.close() doesn't work
        # on socket FDs, but we can't close the socket object normally
        # because we won't get the error we want if the socket knows
        # it's closed.
        server, client = self.make_iostream_pair()
        try:
            os.close(server.socket.fileno())
            if isinstance(server, MicroProxySSLIOStream):
                with self.assertRaises(SSL.SysCallError):
                    server.read_bytes(1, lambda data: None)

            if isinstance(server, MicroProxyIOStream):
                with self.assertRaises(socket.error):
                    server.read_bytes(1, lambda data: None)
        finally:
            server.close()
            client.close()

    def test_async_read_error_logging(self):
        # Socket errors on asynchronous reads should be logged (but only
        # once).
        server, client = self.make_iostream_pair()
        server.set_close_callback(self.stop)
        try:
            # Start a read that will be fulfilled asynchronously.
            server.read_bytes(1, lambda data: None)
            client.write(b'a')
            # Stub out read_from_fd to make it fail.

            def fake_read_from_fd():
                os.close(server.socket.fileno())
                server.__class__.read_from_fd(server)
            server.read_from_fd = fake_read_from_fd
            # This log message is from _handle_read (not read_from_fd).
            self.wait()
        finally:
            server.close()
            client.close()

    def test_future_close_callback(self):
        # Regression test for interaction between the Future read interfaces
        # and MicroProxyIOStream._maybe_add_error_listener.
        server, client = self.make_iostream_pair()
        closed = [False]

        def close_callback():
            closed[0] = True
            self.stop()
        server.set_close_callback(close_callback)
        try:
            client.write(b'a')
            future = server.read_bytes(1)
            self.io_loop.add_future(future, self.stop)
            self.assertEqual(self.wait().result(), b'a')
            self.assertFalse(closed[0])
            client.close()
            self.wait()
            self.assertTrue(closed[0])
        finally:
            server.close()
            client.close()

    def test_read_bytes_partial(self):
        server, client = self.make_iostream_pair()
        try:
            # Ask for more than is available with partial=True
            client.read_bytes(50, self.stop, partial=True)
            server.write(b"hello")
            data = self.wait()
            self.assertEqual(data, b"hello")

            # Ask for less than what is available; num_bytes is still
            # respected.
            client.read_bytes(3, self.stop, partial=True)
            server.write(b"world")
            data = self.wait()
            self.assertEqual(data, b"wor")

            # Partial reads won't return an empty string, but read_bytes(0)
            # will.
            client.read_bytes(0, self.stop, partial=True)
            data = self.wait()
            self.assertEqual(data, b'')
        finally:
            server.close()
            client.close()

    def test_read_until_max_bytes(self):
        server, client = self.make_iostream_pair()
        client.set_close_callback(lambda: self.stop("closed"))
        try:
            # Extra room under the limit
            client.read_until(b"def", self.stop, max_bytes=50)
            server.write(b"abcdef")
            data = self.wait()
            self.assertEqual(data, b"abcdef")

            # Just enough space
            client.read_until(b"def", self.stop, max_bytes=6)
            server.write(b"abcdef")
            data = self.wait()
            self.assertEqual(data, b"abcdef")

            # Not enough space, but we don't know it until all we can do is
            # log a warning and close the connection.
            client.read_until(b"def", self.stop, max_bytes=5)
            server.write(b"123456")
            data = self.wait()
            self.assertEqual(data, "closed")
        finally:
            server.close()
            client.close()

    def test_read_until_max_bytes_inline(self):
        server, client = self.make_iostream_pair()
        client.set_close_callback(lambda: self.stop("closed"))
        try:
            # Similar to the error case in the previous test, but the
            # server writes first so client reads are satisfied
            # inline.  For consistency with the out-of-line case, we
            # do not raise the error synchronously.
            server.write(b"123456")
            client.read_until(b"def", self.stop, max_bytes=5)
            data = self.wait()
            self.assertEqual(data, "closed")
        finally:
            server.close()
            client.close()

    def test_read_until_max_bytes_ignores_extra(self):
        server, client = self.make_iostream_pair()
        client.set_close_callback(lambda: self.stop("closed"))
        try:
            # Even though data that matches arrives the same packet that
            # puts us over the limit, we fail the request because it was not
            # found within the limit.
            server.write(b"abcdef")
            client.read_until(b"def", self.stop, max_bytes=5)
            data = self.wait()
            self.assertEqual(data, "closed")
        finally:
            server.close()
            client.close()

    def test_read_until_regex_max_bytes(self):
        server, client = self.make_iostream_pair()
        client.set_close_callback(lambda: self.stop("closed"))
        try:
            # Extra room under the limit
            client.read_until_regex(b"def", self.stop, max_bytes=50)
            server.write(b"abcdef")
            data = self.wait()
            self.assertEqual(data, b"abcdef")

            # Just enough space
            client.read_until_regex(b"def", self.stop, max_bytes=6)
            server.write(b"abcdef")
            data = self.wait()
            self.assertEqual(data, b"abcdef")

            # Not enough space, but we don't know it until all we can do is
            # log a warning and close the connection.
            client.read_until_regex(b"def", self.stop, max_bytes=5)
            server.write(b"123456")
            data = self.wait()
            self.assertEqual(data, "closed")
        finally:
            server.close()
            client.close()

    def test_read_until_regex_max_bytes_inline(self):
        server, client = self.make_iostream_pair()
        client.set_close_callback(lambda: self.stop("closed"))
        try:
            # Similar to the error case in the previous test, but the
            # server writes first so client reads are satisfied
            # inline.  For consistency with the out-of-line case, we
            # do not raise the error synchronously.
            server.write(b"123456")
            client.read_until_regex(b"def", self.stop, max_bytes=5)
            data = self.wait()
            self.assertEqual(data, "closed")
        finally:
            server.close()
            client.close()

    def test_read_until_regex_max_bytes_ignores_extra(self):
        server, client = self.make_iostream_pair()
        client.set_close_callback(lambda: self.stop("closed"))
        try:
            # Even though data that matches arrives the same packet that
            # puts us over the limit, we fail the request because it was not
            # found within the limit.
            server.write(b"abcdef")
            client.read_until_regex(b"def", self.stop, max_bytes=5)
            data = self.wait()
            self.assertEqual(data, "closed")
        finally:
            server.close()
            client.close()

    def test_small_reads_from_large_buffer(self):
        # 10KB buffer size, 100KB available to read.
        # Read 1KB at a time and make sure that the buffer is not eagerly
        # filled.
        server, client = self.make_iostream_pair(max_buffer_size=10 * 1024)
        try:
            server.write(b"a" * 1024 * 100)
            for i in range(100):
                client.read_bytes(1024, self.stop)
                data = self.wait()
                self.assertEqual(data, b"a" * 1024)
        finally:
            server.close()
            client.close()

    def test_small_read_untils_from_large_buffer(self):
        # 10KB buffer size, 100KB available to read.
        # Read 1KB at a time and make sure that the buffer is not eagerly
        # filled.
        server, client = self.make_iostream_pair(max_buffer_size=10 * 1024)
        try:
            server.write((b"a" * 1023 + b"\n") * 100)
            for i in range(100):
                client.read_until(b"\n", self.stop, max_bytes=4096)
                data = self.wait()
                self.assertEqual(data, b"a" * 1023 + b"\n")
        finally:
            server.close()
            client.close()

    def test_flow_control(self):
        MB = 1024 * 1024
        server, client = self.make_iostream_pair(max_buffer_size=5 * MB)
        try:
            # Client writes more than the server will accept.
            client.write(b"a" * 10 * MB)
            # The server pauses while reading.
            server.read_bytes(MB, self.stop)
            self.wait()
            self.io_loop.call_later(0.1, self.stop)
            self.wait()
            # The client's writes have been blocked; the server can
            # continue to read gradually.
            for i in range(9):
                server.read_bytes(MB, self.stop)
                self.wait()
        finally:
            server.close()
            client.close()


class TestIOStream(TestIOStreamMixin, AsyncTestCase):
    def _make_server_iostream(self, connection, **kwargs):
        return MicroProxyIOStream(connection, **kwargs)

    def _make_client_iostream(self, connection, **kwargs):
        return MicroProxyIOStream(connection, **kwargs)


class TestSSLIOStream(TestIOStreamMixin, AsyncTestCase):
    def _make_server_iostream(self, connection, **kwargs):
        dest_context = _server_ssl_options()
        ssl_sock = SSL.Connection(dest_context,
                                  connection)
        ssl_sock.set_accept_state()
        return MicroProxySSLIOStream(
            ssl_sock, io_loop=self.io_loop, **kwargs)

    def _make_client_iostream(self, connection, **kwargs):
        def verify_cb(conn, x509, err_num, err_depth, err_code):
            return True
        dest_context = _client_ssl_options(SSL.VERIFY_NONE, verify_cb)
        return MicroProxySSLIOStream(
            connection, io_loop=self.io_loop,
            ssl_options=dest_context, **kwargs)


class TestIOStreamStartTLS(AsyncTestCase):
    def setUp(self):
        try:
            super(TestIOStreamStartTLS, self).setUp()
            self.listener, self.port = bind_unused_port()
            self.server_stream = None
            self.server_accepted = Future()
            netutil.add_accept_handler(self.listener, self.accept)
            self.client_stream = MicroProxyIOStream(socket.socket())
            self.io_loop.add_future(self.client_stream.connect(
                ('127.0.0.1', self.port)), self.stop)
            self.wait()
            self.io_loop.add_future(self.server_accepted, self.stop)
            self.wait()
        except Exception as e:
            print(e)
            raise

    def tearDown(self):
        if self.server_stream is not None:
            self.server_stream.close()
        if self.client_stream is not None:
            self.client_stream.close()
        self.listener.close()
        super(TestIOStreamStartTLS, self).tearDown()

    def accept(self, connection, address):
        if self.server_stream is not None:
            self.fail("should only get one connection")
        self.server_stream = MicroProxyIOStream(connection)
        self.server_accepted.set_result(None)

    @gen.coroutine
    def client_send_line(self, line):
        self.client_stream.write(line)
        recv_line = yield self.server_stream.read_until(b"\r\n")
        self.assertEqual(line, recv_line)

    @gen.coroutine
    def server_send_line(self, line):
        self.server_stream.write(line)
        recv_line = yield self.client_stream.read_until(b"\r\n")
        self.assertEqual(line, recv_line)

    def client_start_tls(self, ssl_options=None, server_hostname=None):
        client_stream = self.client_stream
        self.client_stream = None
        return client_stream.start_tls(False, ssl_options, server_hostname)

    def server_start_tls(self, ssl_options=None):
        server_stream = self.server_stream
        self.server_stream = None
        return server_stream.start_tls(True, ssl_options)

    @gen_test
    def test_start_tls_smtp(self):
        def verify_cb(conn, x509, err_num, err_depth, err_code):
            return True
        # This flow is simplified from RFC 3207 section 5.
        # We don't really need all of this, but it helps to make sure
        # that after realistic back-and-forth traffic the buffers end up
        # in a sane state.
        yield self.server_send_line(b"220 mail.example.com ready\r\n")
        yield self.client_send_line(b"EHLO mail.example.com\r\n")
        yield self.server_send_line(b"250-mail.example.com welcome\r\n")
        yield self.server_send_line(b"250 STARTTLS\r\n")
        yield self.client_send_line(b"STARTTLS\r\n")
        yield self.server_send_line(b"220 Go ahead\r\n")
        client_future = self.client_start_tls(
            _client_ssl_options(SSL.VERIFY_NONE, verify_cb))

        server_future = self.server_start_tls(_server_ssl_options())
        self.client_stream = yield client_future
        self.server_stream = yield server_future
        self.assertTrue(isinstance(self.client_stream, MicroProxySSLIOStream))
        self.assertTrue(isinstance(self.server_stream, MicroProxySSLIOStream))
        yield self.client_send_line(b"EHLO mail.example.com\r\n")
        yield self.server_send_line(b"250 mail.example.com welcome\r\n")

    @gen_test
    def test_handshake_fail(self):
        def verify_cb(conn, x509, err_num, err_depth, err_code):
            return False

        server_future = self.server_start_tls(_server_ssl_options())
        client_future = self.client_start_tls(
            _client_ssl_options(SSL.VERIFY_PEER, verify_cb))

        with self.assertRaises(SSL.Error):
            yield client_future
        with self.assertRaises((SSL.Error, socket.error)):
            yield server_future

    @gen_test
    def test_check_hostname(self):
        def verify_cb(conn, x509, err_num, err_depth, err_code):
            return True

        server_future = self.server_start_tls(_server_ssl_options())
        client_future = self.client_start_tls(
            _client_ssl_options(SSL.VERIFY_PEER, verify_cb),
            server_hostname=b'localhost')
        with self.assertRaises(VerificationError):
            yield client_future
        # TODO: server will not raise.
        # with self.assertRaises(Exception):
        yield server_future
