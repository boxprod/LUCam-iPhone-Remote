"""The camera's push channel: instead of asking for every value over and over,
the controller opens one WebSocket to the camera, subscribes to the values it
shows, and the camera sends a message whenever one of them changes.

Blackmagic's "Event Control API":
  GET /control/api/v1/event/list               the properties that can be subscribed to
  WebSocket /control/api/v1/event/websocket    (wss:// on the iPhone app's port 4444)

  -> {"type": "request", "data": {"action": "subscribe", "properties": ["/video/iso", ...]}}
  <- {"type": "response", "data": {"action": "subscribe", "success": true,
                                   "values": {"/video/iso": {"iso": 400}, ...}}}
  <- {"type": "event", "data": {"action": "propertyValueChanged",
                                "property": "/video/iso", "value": {"iso": 800}}}

A value is the same object a GET of that path returns. Writes still go through
PUT/POST. Standard library only: a minimal RFC 6455 client (text frames, ping,
close; no extensions).
"""

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time

from . import api

PATH = api.API_ROOT + '/event/websocket'
PING_EVERY = 10.0   # seconds; the camera counts as gone if nothing comes back for 2x this


class HandshakeError(Exception):
	"""The camera answered, but not with a WebSocket (e.g. no event API: 404)."""


class EventStream(threading.Thread):
	"""One WebSocket to one camera. Calls on_values({path: value}) for the current
	values once subscribed and for every change after, and on_closed(reason) once
	when the connection ends (camera gone, app in the background, stop())."""

	def __init__(self, client, properties, on_values, on_closed, timeout=4.0):
		super().__init__(name='lucam-events-' + client.host, daemon=True)
		self.client = client
		self.properties = list(properties)
		self.on_values = on_values
		self.on_closed = on_closed
		self.timeout = timeout
		self.subscribed = threading.Event()
		self._halt = threading.Event()
		self._sock = None
		self._send_lock = threading.Lock()
		self.messages = 0

	def stop(self):
		self._halt.set()
		try:
			self._send_frame(0x8, b'')
		except Exception:
			pass
		self._close_socket()

	# --- connection -------------------------------------------------------------

	def _connect(self):
		c = self.client
		raw = socket.create_connection((c.host, c.port), timeout=self.timeout)
		if c.scheme == 'https':
			raw = api._UNVERIFIED_SSL.wrap_socket(raw, server_hostname=c.host)
		key = base64.b64encode(os.urandom(16)).decode()
		host = '[{}]'.format(c.host) if ':' in c.host else c.host
		raw.sendall(('GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
					 'Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n').format(PATH, host, c.port, key).encode())
		head = b''
		while b'\r\n\r\n' not in head:
			chunk = raw.recv(4096)
			if not chunk:
				raise HandshakeError('connection closed during the handshake')
			head += chunk
			if len(head) > 65536:
				raise HandshakeError('handshake answer too long')
		head, _, rest = head.partition(b'\r\n\r\n')
		status = head.split(b'\r\n', 1)[0].decode('latin-1')
		if ' 101 ' not in status + ' ':
			raise HandshakeError(status)
		self._sock = raw
		self._buf = rest

	def _close_socket(self):
		s, self._sock = self._sock, None
		if s:
			try:
				s.shutdown(socket.SHUT_RDWR)   # wakes a recv() waiting in run()
			except OSError:
				pass
			try:
				s.close()
			except OSError:
				pass

	# --- frames -------------------------------------------------------------------

	def _send_frame(self, opcode, payload):
		if not self._sock:
			return
		head = bytes([0x80 | opcode])
		n = len(payload)
		if n < 126:
			head += bytes([0x80 | n])
		elif n < 65536:
			head += bytes([0x80 | 126]) + struct.pack('!H', n)
		else:
			head += bytes([0x80 | 127]) + struct.pack('!Q', n)
		mask = os.urandom(4)
		masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
		with self._send_lock:
			self._sock.sendall(head + mask + masked)

	def send_json(self, obj):
		self._send_frame(0x1, json.dumps(obj).encode('utf-8'))

	def _read(self, n):
		while len(self._buf) < n:
			chunk = self._sock.recv(65536)
			if not chunk:
				raise ConnectionError('closed by the camera')
			self._buf += chunk
		data, self._buf = self._buf[:n], self._buf[n:]
		return data

	def _read_frame(self):
		b1, b2 = self._read(2)
		opcode, n = b1 & 0x0F, b2 & 0x7F
		if n == 126:
			n = struct.unpack('!H', self._read(2))[0]
		elif n == 127:
			n = struct.unpack('!Q', self._read(8))[0]
		mask = self._read(4) if b2 & 0x80 else None
		payload = self._read(n)
		if mask:
			payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
		return bool(b1 & 0x80), opcode, payload

	# --- main loop ------------------------------------------------------------------

	def run(self):
		reason = 'stopped'
		try:
			self._connect()
			self._sock.settimeout(PING_EVERY)
			self.send_json({'type': 'request', 'data': {'action': 'subscribe', 'properties': self.properties}})
			last_heard = time.monotonic()
			message = b''
			while not self._halt.is_set():
				try:
					fin, opcode, payload = self._read_frame()
				except socket.timeout:
					if time.monotonic() - last_heard > 2 * PING_EVERY:
						reason = 'no answer for {:.0f} s'.format(2 * PING_EVERY)
						break
					self._send_frame(0x9, b'lucam')   # ping: the camera must pong
					continue
				last_heard = time.monotonic()
				if opcode == 0x8:
					reason = 'closed by the camera'
					break
				if opcode == 0x9:
					self._send_frame(0xA, payload)
					continue
				if opcode in (0x1, 0x2, 0x0):
					message += payload
					if fin:
						self._handle(message)
						message = b''
		except HandshakeError as e:
			reason = 'no event socket ({})'.format(e)
		except Exception as e:   # refused, reset, TLS, timeout while connecting
			if not self._halt.is_set():
				reason = str(e) or type(e).__name__
		finally:
			self._close_socket()
			self.on_closed(reason)

	def _handle(self, raw):
		try:
			msg = json.loads(raw.decode('utf-8'))
		except ValueError:
			return
		self.messages += 1
		data = msg.get('data') or {}
		if msg.get('type') == 'response' and data.get('action') == 'subscribe':
			if data.get('success', True):
				self.subscribed.set()
				self.on_values(dict(data.get('values') or {}))   # even none: the subscription is up
		elif msg.get('type') == 'event' and data.get('action') == 'propertyValueChanged' and data.get('property'):
			self.on_values({data['property']: data.get('value')})
