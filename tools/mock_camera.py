#!/usr/bin/env python3
"""Simulated Blackmagic camera(s) speaking the REST API, for testing without
iPhones. Each instance listens on its own port and can register itself with
Bonjour (macOS `dns-sd`) so discovery can be tested too.

    python3 tools/mock_camera.py --count 3 --base-port 8081 --bonjour

Like the app, it also pushes changes over a WebSocket (/event/websocket) to
clients that subscribed; --no-events turns that off (an older app without it).
"""

import argparse
import base64
import hashlib
import json
import select
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = '/control/api/v1'
# The lens list an iPhone 17 Pro Max reports (two front lenses, '77mm' id at 100 mm).
LENSES = [
	{'id': 'Lens13mm', 'focalLength': 13, 'zoomFactor': '0.5x', 'facing': 'back'},
	{'id': 'Lens24mm', 'focalLength': 24, 'zoomFactor': '1x', 'facing': 'back'},
	{'id': 'LensWASecondary', 'focalLength': 48, 'zoomFactor': '2x', 'facing': 'back'},
	{'id': 'Lens77mm', 'focalLength': 100, 'zoomFactor': '4x', 'facing': 'back'},
	{'id': 'Lens200mm', 'focalLength': 200, 'zoomFactor': '8x', 'facing': 'back'},
	{'id': 'LensFrontUltraWide', 'focalLength': 19, 'zoomFactor': '0.5x', 'facing': 'front'},
	{'id': 'LensFront', 'focalLength': 19, 'zoomFactor': '0.5x', 'facing': 'front'},
]
CODECS = ['Apple ProRes 422 HQ:Default', 'Apple ProRes 422:Default', 'HEVC (H.265):High', 'H.264:High', 'H.264:Medium']


def make_state(name, iris_controllable=True):
	return {
		'activeLens': 'Lens24mm',
		'iris': 2.8 if iris_controllable else 1.78,
		'irisControllable': iris_controllable,
		'product': {'deviceName': name, 'productName': 'Blackmagic Camera (iPhone, simulated)', 'softwareVersion': '3.5.3'},
		'recording': False,
		'recStart': 0,
		'iso': 400,
		'autoExposure': 'Off',
		'autoLens': False,
		'supportedISOs': [50, 100, 200, 400, 800, 1250, 1600, 2000, 3200],
		'shutterSpeed': 50,
		'wb': 5600, 'tint': 0,
		'format': {'codec': 'Apple ProRes 422 HQ:Default', 'frameRate': '25',
				   'recordResolution': {'width': 3840, 'height': 2160}, 'sensorResolution': {'width': 3840, 'height': 2160},
				   'resolutionDescriptor': {'group': '4K', 'aspectRatio': '16:9', 'description': '4K'},
				   'offSpeedEnabled': False},
		'zoom': 24,
		'slate': {'clip': {'clipName': 'A001_C001', 'reel': 1, 'scene': '1', 'take': 1},
				  'project': {'projectName': 'Mock', 'camera': 'A'}},
		'stream': 'Idle',
		# like the iPhone app: the screen has a LUT switch, HDMI has LUT and clean feed
		'monitor': {('Device', 'displayLUT'): False, ('HDMI', 'displayLUT'): False, ('HDMI', 'cleanFeed'): False},
	}


def routes(s):
	"""What a GET of each path answers (also what the WebSocket pushes)."""
	tc_secs = int(time.time() - s['recStart']) if s['recording'] else 0
	return {
		'/system/product': s['product'],
		'/system/format': s['format'],
		'/system/supportedFormats': {'supportedFormats': [{
			'codecs': CODECS,
			'frameRates': ['23.98', '24', '25', '29.97', '30', '48', '50', '59.94', '60'],
			'recordResolution': {'width': w, 'height': h}, 'sensorResolution': {'width': w, 'height': h},
			'resolutionDescriptor': {'group': d, 'aspectRatio': '16:9', 'description': d}}
			for w, h, d in ((3840, 2160, '4K'), (1920, 1080, 'HD'))]},
		'/transports/0': {'mode': 'InputRecord' if s['recording'] else 'InputPreview'},
		'/transports/0/record': {'recording': s['recording']},
		'/transports/0/timecode': {'display': '00:00:{:02d}:{:02d}'.format(tc_secs // 60 % 60, tc_secs % 60)},
		'/video/iso': {'iso': s['iso']},
		'/video/autoExposure': {'mode': s['autoExposure']},
		'/lens/cameras/auto': {'supported': True, 'enabled': s['autoLens']},
		'/video/supportedISOs': {'supportedISOs': s['supportedISOs']},
		'/video/shutter': {'continuousShutterAutoExposure': False, 'shutterSpeed': s['shutterSpeed']},
		'/video/supportedShutters': {'shutterSpeeds': [24, 25, 30, 48, 50, 60, 100, 120, 200, 250, 500, 1000]},
		'/video/whiteBalance': {'whiteBalance': s['wb']},
		'/video/whiteBalance/description': {'whiteBalance': {'min': 2500, 'max': 10000}},
		'/video/whiteBalanceTint': {'whiteBalanceTint': s['tint']},
		'/video/whiteBalanceTint/description': {'whiteBalanceTint': {'min': -50, 'max': 50}},
		'/lens/zoom': {'focalLength': s['zoom'], 'normalised': 0.1},
		'/lens/cameras': {'cameras': [dict(l, index=i, isAvailable=True, isActive=l['id'] == s['activeLens'])
									 for i, l in enumerate(LENSES)]},
		'/lens/cameras/active': dict(next(l for l in LENSES if l['id'] == s['activeLens']), isActive=True, isAvailable=True),
		'/lens/iris': {'apertureStop': s['iris'],
					   'continuousApertureAutoExposure': s['autoExposure'] != 'Off' and s['irisControllable']},
		'/lens/iris/description': {'controllable': s['irisControllable'],
								   'apertureStop': {'min': 1.6, 'max': 8.0} if s['irisControllable'] else {'min': 1.78, 'max': 1.78}},
		'/lens/zoom/description': {'controllable': True, 'focalLength': {'adjustable': True, 'min': 13, 'max': 120}},
		'/slates/nextClip': s['slate'],
		'/monitoring/display': {'displays': ['Device']},
		'/monitoring/Device/displayLUT': {'enabled': s['monitor'][('Device', 'displayLUT')]},
		'/monitoring/HDMI/displayLUT': {'enabled': s['monitor'][('HDMI', 'displayLUT')]},
		'/monitoring/HDMI/cleanFeed': {'enabled': s['monitor'][('HDMI', 'cleanFeed')]},
		'/livestreams/0': {'status': s['stream'], 'bitrate': 0, 'effectiveVideoFormat': '1080p25'},
	}


# What the app lets a client subscribe to (the phone lists the screen as LCD,
# so its Device LUT switch can't be subscribed to and has to be polled).
EVENTS = ['/transports/0/record', '/transports/0/timecode', '/video/iso', '/video/shutter', '/video/whiteBalance',
		  '/video/whiteBalanceTint', '/video/autoExposure', '/system/format', '/lens/iris', '/lens/zoom',
		  '/lens/cameras/active', '/slates/nextClip', '/livestreams/0', '/monitoring/LCD/displayLUT',
		  '/monitoring/HDMI/displayLUT', '/monitoring/HDMI/cleanFeed']
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def make_handler(state, lock, verbose, events=True):
	class Handler(BaseHTTPRequestHandler):
		protocol_version = 'HTTP/1.1'

		def log_message(self, fmt, *args):
			if verbose:
				super().log_message(fmt, *args)

		def _send(self, code, payload=None):
			body = json.dumps(payload).encode() if payload is not None else b''
			self.send_response(code)
			if body:
				self.send_header('Content-Type', 'application/json')
			self.send_header('Content-Length', str(len(body)))
			self.end_headers()
			self.wfile.write(body)

		def _body(self):
			n = int(self.headers.get('Content-Length') or 0)
			return json.loads(self.rfile.read(n)) if n else {}

		def _path(self):
			if not self.path.startswith(ROOT):
				return None
			return self.path[len(ROOT):].split('?')[0]

		def do_GET(self):
			p = self._path()
			if p == '/event/websocket' and 'websocket' in (self.headers.get('Upgrade') or '').lower() and events:
				return self._websocket()
			with lock:
				answers = routes(state)
				if events:
					answers['/event/list'] = {'events': EVENTS}
			if p in answers:
				self._send(200, answers[p])
			else:
				self._send(404, {'error': 'not found'})

		def _websocket(self):
			"""The push channel: subscribe, then a message per changed value."""
			accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] + WS_GUID).encode()).digest()).decode()
			self.send_response(101)
			self.send_header('Upgrade', 'websocket')
			self.send_header('Connection', 'Upgrade')
			self.send_header('Sec-WebSocket-Accept', accept)
			self.end_headers()
			self.wfile.flush()
			conn, buf = self.connection, b''
			subscribed, sent = [], {}

			def send(obj, opcode=0x1):
				data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
				n = len(data)
				head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack('!H', n)
												  if n < 65536 else bytes([127]) + struct.pack('!Q', n))
				conn.sendall(head + data)

			send({'type': 'event', 'data': {'action': 'websocketOpened'}})
			try:
				while True:
					ready = select.select([conn], [], [], 0.25)[0]
					if ready:
						chunk = conn.recv(65536)
						if not chunk:
							return
						buf += chunk
						while len(buf) >= 2:
							n, at = buf[1] & 0x7F, 2
							if n == 126:
								n, at = struct.unpack('!H', buf[2:4])[0], 4
							elif n == 127:
								n, at = struct.unpack('!Q', buf[2:10])[0], 10
							if len(buf) < at + 4 + n:
								break
							mask, payload = buf[at:at + 4], buf[at + 4:at + 4 + n]
							payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
							opcode, buf = buf[0] & 0x0F, buf[at + 4 + n:]
							if opcode == 0x8:
								send(b'', 0x8)
								return
							if opcode == 0x9:
								send(payload, 0xA)
							elif opcode == 0x1:
								req = (json.loads(payload).get('data') or {})
								if req.get('action') == 'subscribe':
									props = [x for x in req.get('properties', []) if x in EVENTS]
									subscribed.extend(x for x in props if x not in subscribed)
									with lock:
										now = routes(state)
									values = {x: now[x] for x in props if x in now}
									sent.update({x: json.dumps(v) for x, v in values.items()})
									send({'type': 'response', 'data': {'action': 'subscribe', 'success': True,
																	   'properties': props, 'values': values}})
					with lock:
						now = routes(state)
					for x in subscribed:
						v = json.dumps(now.get(x))
						if sent.get(x) != v:
							sent[x] = v
							send({'type': 'event', 'data': {'action': 'propertyValueChanged', 'property': x, 'value': now.get(x)}})
			except (OSError, ValueError):
				return

		def do_PUT(self):
			self._write('PUT')

		def do_POST(self):
			self._write('POST')

		def _write(self, method):
			p = self._path()
			body = self._body()
			s = state
			with lock:
				if p == '/transports/0/record':
					rec = body.get('recording', True) if method == 'PUT' else True
					if rec and not s['recording']:
						s['recStart'] = time.time()
						c = s['slate']['clip']
						c['clipName'] = '{}{:03d}_C{:03d}'.format(s['slate']['project']['camera'], c['reel'], c['take'])
					if not rec and s['recording']:
						s['slate']['clip']['take'] += 1
					s['recording'] = rec
				elif p == '/transports/0/stop' and method == 'POST':
					if s['recording']:
						s['slate']['clip']['take'] += 1
					s['recording'] = False
				elif p == '/video/iso':
					if body['iso'] not in s['supportedISOs']:
						return self._send(400, {'error': 'unsupported ISO'})
					s['iso'] = body['iso']
					s['autoExposure'] = 'Off'   # like the app: a manual ISO or shutter ends auto exposure
				elif p == '/video/shutter':
					if 'shutterSpeed' not in body:
						return self._send(400, {'error': 'this mock only supports shutter speed'})
					s['shutterSpeed'] = body['shutterSpeed']
					s['autoExposure'] = 'Off'
				elif p == '/video/autoExposure':
					if body.get('mode') not in ('Off', 'Continuous', 'OneShot'):
						return self._send(400, {'error': 'bad mode'})
					s['autoExposure'] = body['mode']
				elif p == '/lens/cameras/auto':
					s['autoLens'] = bool(body.get('enabled'))
				elif p == '/video/whiteBalance':
					if not 2500 <= body['whiteBalance'] <= 10000:
						return self._send(400, {'error': 'Invalid white balance temperature'})
					s['wb'] = body['whiteBalance']
				elif p == '/video/whiteBalance/doAuto':
					s['wb'] = 5200
				elif p == '/video/whiteBalanceTint':
					s['tint'] = max(-50, min(50, body['whiteBalanceTint']))
				elif p == '/system/format':
					if s['recording']:
						return self._send(409, {'error': 'cannot change format while recording'})
					s['format'].update(body)
				elif p == '/lens/cameras/active':
					lens = next((l for l in LENSES if l['id'] == body.get('id')), None)
					if lens is None:
						return self._send(400, {'error': 'unknown lens'})
					s['activeLens'] = lens['id']
					s['zoom'] = lens['focalLength']
				elif p == '/lens/iris':
					if not s['irisControllable'] or s['autoExposure'] != 'Off':
						return self._send(403, {'error': 'lens iris is not controllable'})
					if not 1.6 <= body.get('apertureStop', 0) <= 8.0:
						return self._send(400, {'error': 'out of range'})
					s['iris'] = body['apertureStop']
				elif p == '/lens/zoom':
					s['zoom'] = max(13, min(120, body.get('focalLength', s['zoom'])))
				elif p == '/slates/nextClip':
					for k, v in body.items():
						s['slate'].setdefault(k, {}).update(v)
					return self._send(200, s['slate'])
				elif p.startswith('/monitoring/') and tuple(p.split('/')[2:]) in s['monitor']:
					s['monitor'][tuple(p.split('/')[2:])] = bool(body.get('enabled'))
				elif p == '/livestreams/0/start':
					s['stream'] = 'Streaming'
				elif p == '/livestreams/0/stop':
					s['stream'] = 'Idle'
				else:
					return self._send(404, {'error': 'not found'})
			self._send(204)

	return Handler


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument('--count', type=int, default=1)
	ap.add_argument('--base-port', type=int, default=8081)
	ap.add_argument('--bonjour', action='store_true', help='register each mock with dns-sd (macOS)')
	ap.add_argument('-v', '--verbose', action='store_true')
	ap.add_argument('--no-events', action='store_true', help='no WebSocket push channel (like an older app)')
	args = ap.parse_args()

	procs = []
	for i in range(args.count):
		port = args.base_port + i
		name = 'Mock iPhone {}'.format(chr(ord('A') + i))
		# even-numbered mocks have an adjustable iris, odd ones a fixed f/1.78 like most iPhones
		server = ThreadingHTTPServer(('0.0.0.0', port), make_handler(make_state(name, i % 2 == 0), threading.Lock(), args.verbose,
																			   not args.no_events))
		threading.Thread(target=server.serve_forever, daemon=True).start()
		print('{} on http://127.0.0.1:{}'.format(name, port), flush=True)
		if args.bonjour:
			procs.append(subprocess.Popen(['dns-sd', '-R', name, '_http._tcp', 'local', str(port)],
										  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
	try:
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		pass
	finally:
		for p in procs:
			p.terminate()


if __name__ == '__main__':
	main()
