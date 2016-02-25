#!/usr/bin/env python

# Copyright (C) 2007-2016 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

import contextlib
import errno
import select
import socket
import time

from pyftpdlib.ioloop import Acceptor
from pyftpdlib.ioloop import AsyncChat
from pyftpdlib.ioloop import IOLoop
from pyftpdlib.ioloop import RetryError
from pyftpdlib.test import mock
from pyftpdlib.test import POSIX
from pyftpdlib.test import unittest
from pyftpdlib.test import VERBOSITY
import pyftpdlib.ioloop


if hasattr(socket, 'socketpair'):
    socketpair = socket.socketpair
else:
    def socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        with contextlib.closing(socket.socket(family, type, proto)) as l:
            l.bind(("localhost", 0))
            l.listen()
            c = socket.socket(family, type, proto)
            try:
                c.connect(l.getsockname())
                caddr = c.getsockname()
                while True:
                    a, addr = l.accept()
                    # check that we've got the correct client
                    if addr == caddr:
                        return c, a
                    a.close()
            except OSError:
                c.close()
                raise


# TODO: write more tests.
class BaseIOLoopTestCase(object):

    ioloop_class = None

    def make_socketpair(self):
        rd, wr = socketpair()
        self.addCleanup(rd.close)
        self.addCleanup(wr.close)
        return rd, wr

    def test_register(self):
        s = self.ioloop_class()
        self.addCleanup(s.close)
        rd, wr = self.make_socketpair()
        handler = AsyncChat(rd)
        s.register(rd, handler, s.READ)
        s.register(wr, handler, s.WRITE)
        self.assertIn(rd, s.socket_map)
        self.assertIn(wr, s.socket_map)
        return (s, rd, wr)

    def test_unregister(self):
        s, rd, wr = self.test_register()
        s.unregister(rd)
        s.unregister(wr)
        self.assertNotIn(rd, s.socket_map)
        self.assertNotIn(wr, s.socket_map)

    def test_unregister_twice(self):
        s, rd, wr = self.test_register()
        s.unregister(rd)
        s.unregister(rd)
        s.unregister(wr)
        s.unregister(wr)

    def test_modify(self):
        s, rd, wr = self.test_register()
        s.modify(rd, s.WRITE)
        s.modify(wr, s.READ)

    def test_loop(self):
        # no timeout
        s, rd, wr = self.test_register()
        s.call_later(0, s.close)
        s.loop()
        # with timeout
        s, rd, wr = self.test_register()
        s.call_later(0, s.close)
        s.loop(timeout=0.001)

    def test_close(self):
        s, rd, wr = self.test_register()
        s.close()
        self.assertEqual(s.socket_map, {})

    def test_close_w_handler_exc(self):
        # Simulate an exception when close()ing a socket handler.
        # Exception should be logged and ignored.
        class Handler(AsyncChat):
            def close(self):
                1 / 0

        s = self.ioloop_class()
        self.addCleanup(s.close)
        rd, wr = self.make_socketpair()
        handler = Handler(rd)
        s.register(rd, handler, s.READ)
        with mock.patch("pyftpdlib.ioloop.logger.error") as m:
            s.close()
            assert m.called
            self.assertIn('ZeroDivisionError', m.call_args[0][0])

    def test_close_w_handler_ebadf_exc(self):
        # Simulate an exception when close()ing a socket handler.
        # Exception should be ignored (and not logged).
        class Handler(AsyncChat):
            def close(self):
                raise OSError(errno.EBADF, "")

        s = self.ioloop_class()
        self.addCleanup(s.close)
        rd, wr = self.make_socketpair()
        handler = Handler(rd)
        s.register(rd, handler, s.READ)
        with mock.patch("pyftpdlib.ioloop.logger.error") as m:
            s.close()
            assert not m.called

    def test_close_w_callback_exc(self):
        # Simulate an exception when close()ing the IO loop and a
        # scheduled callback raises an exception on cancel().
        with mock.patch("pyftpdlib.ioloop.logger.error") as logerr:
            with mock.patch("pyftpdlib.ioloop._CallLater.cancel",
                            side_effect=lambda: 1 / 0) as cancel:
                s = self.ioloop_class()
                self.addCleanup(s.close)
                s.call_later(1, lambda: 0)
                s.close()
                assert cancel.called
                assert logerr.called
                self.assertIn('ZeroDivisionError', logerr.call_args[0][0])


class DefaultIOLoopTestCase(unittest.TestCase, BaseIOLoopTestCase):
    ioloop_class = pyftpdlib.ioloop.IOLoop


# ===================================================================
# select()
# ===================================================================

class SelectIOLoopTestCase(unittest.TestCase, BaseIOLoopTestCase):
    ioloop_class = pyftpdlib.ioloop.Select

    def test_select_eintr(self):
        # EINTR is supposed to be ignored
        with mock.patch('pyftpdlib.ioloop.select.select',
                        side_effect=select.error()) as m:
            m.side_effect.errno = errno.EINTR
            s, rd, wr = self.test_register()
            s.poll(0)
        # ...but just that
        with mock.patch('pyftpdlib.ioloop.select.select',
                        side_effect=select.error()) as m:
            m.side_effect.errno = errno.EBADF
            s, rd, wr = self.test_register()
            self.assertRaises(select.error, s.poll, 0)


# ===================================================================
# poll()
# ===================================================================

@unittest.skipUnless(hasattr(pyftpdlib.ioloop, 'Poll'),
                     "poll() not available on this platform")
class PollIOLoopTestCase(unittest.TestCase, BaseIOLoopTestCase):
    ioloop_class = getattr(pyftpdlib.ioloop, "Poll", None)

    def test_poll_eintr(self, ):
        # EINTR is supposed to be ignored
        with mock.patch("pyftpdlib.ioloop.Poll._poller",
                        return_vaue=mock.Mock()) as m_poll:
            m_poll.return_value.poll.side_effect = select.error
            m_poll.return_value.poll.side_effect.errno = errno.EINTR
            s, rd, wr = self.test_register()
            s.poll(0)
        # ...but just that
        with mock.patch("pyftpdlib.ioloop.Poll._poller",
                        return_vaue=mock.Mock()) as m_poll:
            m_poll.return_value.poll.side_effect = select.error
            m_poll.return_value.poll.side_effect.errno = errno.EBADF
            s, rd, wr = self.test_register()
            self.assertRaises(select.error, s.poll, 0)


# ===================================================================
# epoll()
# ===================================================================

@unittest.skipUnless(hasattr(pyftpdlib.ioloop, 'Epoll'),
                     "epoll() not available on this platform (Linux only)")
class EpollIOLoopTestCase(unittest.TestCase, BaseIOLoopTestCase):
    ioloop_class = getattr(pyftpdlib.ioloop, "Epoll", None)

    def test_epoll_eintr(self):
        # EINTR is supposed to be ignored
        with mock.patch("pyftpdlib.ioloop.Epoll._poller",
                        return_vaue=mock.Mock()) as m_poll:
            m_poll.return_value.poll.side_effect = select.error
            m_poll.return_value.poll.side_effect.errno = errno.EINTR
            s, rd, wr = self.test_register()
            s.poll(0)
        # ...but just that
        with mock.patch("pyftpdlib.ioloop.Epoll._poller",
                        return_vaue=mock.Mock()) as m_poll:
            m_poll.return_value.poll.side_effect = select.error
            m_poll.return_value.poll.side_effect.errno = errno.EBADF
            s, rd, wr = self.test_register()
            self.assertRaises(select.error, s.poll, 0)


# ===================================================================
# /dev/poll
# ===================================================================

@unittest.skipUnless(hasattr(pyftpdlib.ioloop, 'DevPoll'),
                     "/dev/poll not available on this platform (Solaris only)")
class DevPollIOLoopTestCase(unittest.TestCase, BaseIOLoopTestCase):
    ioloop_class = getattr(pyftpdlib.ioloop, "DevPoll", None)


# ===================================================================
# kqueue
# ===================================================================

@unittest.skipUnless(hasattr(pyftpdlib.ioloop, 'Kqueue'),
                     "/dev/poll not available on this platform (BSD only)")
class KqueueIOLoopTestCase(unittest.TestCase, BaseIOLoopTestCase):
    ioloop_class = getattr(pyftpdlib.ioloop, "Kqueue", None)


class TestCallLater(unittest.TestCase):
    """Tests for CallLater class."""

    def setUp(self):
        self.ioloop = IOLoop.instance()
        for task in self.ioloop.sched._tasks:
            if not task.cancelled:
                task.cancel()
        del self.ioloop.sched._tasks[:]

    def scheduler(self, timeout=0.01, count=100):
        while self.ioloop.sched._tasks and count > 0:
            self.ioloop.sched.poll()
            count -= 1
            time.sleep(timeout)

    def test_interface(self):
        def fun():
            return 0

        self.assertRaises(AssertionError, self.ioloop.call_later, -1, fun)
        x = self.ioloop.call_later(3, fun)
        self.assertEqual(x.cancelled, False)
        x.cancel()
        self.assertEqual(x.cancelled, True)
        self.assertRaises(AssertionError, x.call)
        self.assertRaises(AssertionError, x.reset)
        x.cancel()

    def test_order(self):
        def fun(x):
            l.append(x)

        l = []
        for x in [0.05, 0.04, 0.03, 0.02, 0.01]:
            self.ioloop.call_later(x, fun, x)
        self.scheduler()
        self.assertEqual(l, [0.01, 0.02, 0.03, 0.04, 0.05])

    # The test is reliable only on those systems where time.time()
    # provides time with a better precision than 1 second.
    if not str(time.time()).endswith('.0'):
        def test_reset(self):
            def fun(x):
                l.append(x)

            l = []
            self.ioloop.call_later(0.01, fun, 0.01)
            self.ioloop.call_later(0.02, fun, 0.02)
            self.ioloop.call_later(0.03, fun, 0.03)
            x = self.ioloop.call_later(0.04, fun, 0.04)
            self.ioloop.call_later(0.05, fun, 0.05)
            time.sleep(0.1)
            x.reset()
            self.scheduler()
            self.assertEqual(l, [0.01, 0.02, 0.03, 0.05, 0.04])

    def test_cancel(self):
        def fun(x):
            l.append(x)

        l = []
        self.ioloop.call_later(0.01, fun, 0.01).cancel()
        self.ioloop.call_later(0.02, fun, 0.02)
        self.ioloop.call_later(0.03, fun, 0.03)
        self.ioloop.call_later(0.04, fun, 0.04)
        self.ioloop.call_later(0.05, fun, 0.05).cancel()
        self.scheduler()
        self.assertEqual(l, [0.02, 0.03, 0.04])

    def test_errback(self):
        l = []
        self.ioloop.call_later(
            0.0, lambda: 1 // 0, _errback=lambda: l.append(True))
        self.scheduler()
        self.assertEqual(l, [True])

    def test__repr__(self):
        repr(self.ioloop.call_later(0.01, lambda: 0, 0.01))

    def test__lt__(self):
        a = self.ioloop.call_later(0.01, lambda: 0, 0.01)
        b = self.ioloop.call_later(0.02, lambda: 0, 0.02)
        self.assertTrue(a < b)

    def test__le__(self):
        a = self.ioloop.call_later(0.01, lambda: 0, 0.01)
        b = self.ioloop.call_later(0.02, lambda: 0, 0.02)
        self.assertTrue(a <= b)


class TestCallEvery(unittest.TestCase):
    """Tests for CallEvery class."""

    def setUp(self):
        self.ioloop = IOLoop.instance()
        for task in self.ioloop.sched._tasks:
            if not task.cancelled:
                task.cancel()
        del self.ioloop.sched._tasks[:]

    def scheduler(self, timeout=0.003):
        stop_at = time.time() + timeout
        while time.time() < stop_at:
            self.ioloop.sched.poll()

    def test_interface(self):
        def fun():
            return 0

        self.assertRaises(AssertionError, self.ioloop.call_every, -1, fun)
        x = self.ioloop.call_every(3, fun)
        self.assertEqual(x.cancelled, False)
        x.cancel()
        self.assertEqual(x.cancelled, True)
        self.assertRaises(AssertionError, x.call)
        self.assertRaises(AssertionError, x.reset)
        x.cancel()

    def test_only_once(self):
        # make sure that callback is called only once per-loop
        def fun():
            l1.append(None)

        l1 = []
        self.ioloop.call_every(0, fun)
        self.ioloop.sched.poll()
        self.assertEqual(l1, [None])

    def test_multi_0_timeout(self):
        # make sure a 0 timeout callback is called as many times
        # as the number of loops
        def fun():
            l.append(None)

        l = []
        self.ioloop.call_every(0, fun)
        for x in range(100):
            self.ioloop.sched.poll()
        self.assertEqual(len(l), 100)

    # run it on systems where time.time() has a higher precision
    if POSIX:
        def test_low_and_high_timeouts(self):
            # make sure a callback with a lower timeout is called more
            # frequently than another with a greater timeout
            def fun():
                l1.append(None)

            l1 = []
            self.ioloop.call_every(0.001, fun)
            self.scheduler()

            def fun():
                l2.append(None)

            l2 = []
            self.ioloop.call_every(0.005, fun)
            self.scheduler(timeout=0.01)

            self.assertTrue(len(l1) > len(l2))

    def test_cancel(self):
        # make sure a cancelled callback doesn't get called anymore
        def fun():
            l.append(None)

        l = []
        call = self.ioloop.call_every(0.001, fun)
        self.scheduler()
        len_l = len(l)
        call.cancel()
        self.scheduler()
        self.assertEqual(len_l, len(l))

    def test_errback(self):
        l = []
        self.ioloop.call_every(
            0.0, lambda: 1 // 0, _errback=lambda: l.append(True))
        self.scheduler()
        self.assertTrue(l)


class TestAsyncChat(unittest.TestCase):

    def get_connected_handler(self):
        s = socket.socket()
        self.addCleanup(s.close)
        ac = AsyncChat(sock=s)
        self.addCleanup(ac.close)
        return ac

    def test_send_retry(self):
        ac = self.get_connected_handler()
        for errnum in pyftpdlib.ioloop._ERRNOS_RETRY:
            with mock.patch("pyftpdlib.ioloop.socket.socket.send",
                            side_effect=socket.error(errnum, "")) as m:
                self.assertEqual(ac.send(b"x"), 0)
                assert m.called

    def test_send_disconnect(self):
        ac = self.get_connected_handler()
        for errnum in pyftpdlib.ioloop._ERRNOS_DISCONNECTED:
            with mock.patch("pyftpdlib.ioloop.socket.socket.send",
                            side_effect=socket.error(errnum, "")) as send:
                with mock.patch.object(ac, "handle_close") as handle_close:
                    self.assertEqual(ac.send(b"x"), 0)
                    assert send.called
                    assert handle_close.called

    def test_recv_retry(self):
        ac = self.get_connected_handler()
        for errnum in pyftpdlib.ioloop._ERRNOS_RETRY:
            with mock.patch("pyftpdlib.ioloop.socket.socket.recv",
                            side_effect=socket.error(errnum, "")) as m:
                self.assertRaises(RetryError, ac.recv, 1024)
                assert m.called

    def test_recv_disconnect(self):
        ac = self.get_connected_handler()
        for errnum in pyftpdlib.ioloop._ERRNOS_DISCONNECTED:
            with mock.patch("pyftpdlib.ioloop.socket.socket.recv",
                            side_effect=socket.error(errnum, "")) as send:
                with mock.patch.object(ac, "handle_close") as handle_close:
                    self.assertEqual(ac.recv(b"x"), b'')
                    assert send.called
                    assert handle_close.called

    def test_connect_af_unspecified_err(self):
        ac = AsyncChat()
        with mock.patch.object(
                ac, "connect",
                side_effect=socket.error(errno.EBADF, "")) as m:
            self.assertRaises(socket.error,
                              ac.connect_af_unspecified, ("localhost", 0))
            assert m.called
            self.assertIsNone(ac.socket)


class TestAcceptor(unittest.TestCase):

    def test_bind_af_unspecified_err(self):
        ac = Acceptor()
        with mock.patch.object(
                ac, "bind",
                side_effect=socket.error(errno.EBADF, "")) as m:
            self.assertRaises(socket.error,
                              ac.bind_af_unspecified, ("localhost", 0))
            assert m.called
            self.assertIsNone(ac.socket)

    def test_handle_accept_econnacorted(self):
        # https://github.com/giampaolo/pyftpdlib/issues/105
        ac = Acceptor()
        with mock.patch.object(
                ac, "accept",
                side_effect=socket.error(errno.ECONNABORTED, "")) as m:
            ac.handle_accept()
            assert m.called
            self.assertIsNone(ac.socket)

    def test_handle_accept_typeerror(self):
        # https://github.com/giampaolo/pyftpdlib/issues/91
        ac = Acceptor()
        with mock.patch.object(ac, "accept", side_effect=TypeError) as m:
            ac.handle_accept()
            assert m.called
            self.assertIsNone(ac.socket)


if __name__ == '__main__':
    unittest.main(verbosity=VERBOSITY)
