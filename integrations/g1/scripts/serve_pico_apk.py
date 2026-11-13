#!/usr/bin/env python3
"""Serve the PICO installer and isolated, read-only wrist-distance display."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from network_config import laptop_host

ROOT = Path(__file__).resolve().parents[1]


def handler_for(apk, feed, *, root=ROOT, host=None, port=8027, token=None):
    host = host or laptop_host(root)
    apk=Path(apk).resolve()
    token=token or secrets.token_urlsafe(32)
    digest=hashlib.sha256(apk.read_bytes()).hexdigest();size=apk.stat().st_size
    html=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>G1 Wrists · Two Object Trackers</title><style>body{{font:20px/1.55 system-ui,sans-serif;background:#eef2f5;color:#142c3b;margin:0;padding:36px}}main{{max-width:820px;margin:auto;background:white;border-radius:20px;padding:36px}}h1{{line-height:1.2}}a.button{{display:block;background:#147d6d;color:white;text-decoration:none;padding:22px;margin:16px 0;text-align:center;border-radius:12px;font-size:24px}}code{{overflow-wrap:anywhere;font-size:14px}}li{{margin:14px 0}}.note{{background:#eef7f5;padding:18px;border-radius:12px}}</style><main><p>PICO · G1 TELEOP</p><a class="button" href="/distance">实时腕部位移 · Distance for calibration</a><h1>Two trackers in Object mode</h1><p>XRoboToolkit G1 Wrists is a separate local build. The existing app stays installed.</p><a class="button" href="/XRoboToolkit-G1-Wrists-TwoObject.apk">Download APK · {size/1024/1024:.1f} MB</a><ol><li>Open this page in the PICO browser and download the APK. Open the download and finish Android's installation prompt.</li><li>Close the original XRoboToolkit app. Open <strong>XRoboToolkit G1 Wrists</strong>.</li><li>Select <strong>Object</strong>, complete the PICO prompt for <strong>two trackers</strong>, enable <strong>Head</strong>, connect to <strong>{host}</strong>, and turn on <strong>Send</strong>.</li><li>Return to the laptop dashboard to check the left and right wrist inputs and complete arm calibration.</li></ol><p class="note">This build explicitly requests two trackers whenever Object mode is selected. The laptop dashboard shows the current wrist inputs and calibration status.</p><p>Package: <code>com.xrobotoolkit.client.g1wrists</code></p><p>SHA-256: <code>{digest}</code></p></main></html>'''.encode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self, status, body, kind='application/json; charset=utf-8'):
            if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            if self.path.split('?',1)[0]!='/':
                self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def expected_host(self):
            return self.headers.get('Host') in (f'{host}:{port}',f'127.0.0.1:{port}',f'localhost:{port}')
        def do_GET(self):
            if not self.expected_host():return self.respond(403,{'error':'Unexpected host'})
            path=urlsplit(self.path).path
            if path=='/':body,kind=html,'text/html; charset=utf-8'
            elif path=='/sha256':body,kind=(digest+'  '+apk.name+'\n').encode(),'text/plain; charset=utf-8'
            elif path in ('/distance','/distance.js','/distance.css'):
                name,kind={'/distance':('pico-distance.html','text/html; charset=utf-8'),
                    '/distance.js':('pico-distance.js','text/javascript; charset=utf-8'),
                    '/distance.css':('pico-distance.css','text/css; charset=utf-8')}[path]
                return self.respond(200,(Path(root)/'web'/name).read_bytes(),kind)
            elif path=='/api/distance':return self.respond(200,{**feed.status(),'token':token,'laptop_ip':host})
            elif path=='/distance.jpg':return self.respond(200,feed.jpeg(),'image/jpeg')
            elif path=='/favicon.ico':return self.respond(204,b'','image/x-icon')
            elif path=='/XRoboToolkit-G1-Wrists-TwoObject.apk':
                self.send_response(200);self.send_header('Content-Type','application/vnd.android.package-archive');self.send_header('Content-Length',str(size));self.send_header('Content-Disposition','attachment; filename="XRoboToolkit-G1-Wrists-TwoObject.apk"');self.end_headers()
                try:
                    with apk.open('rb') as source:shutil.copyfileobj(source,self.wfile)
                except (BrokenPipeError,ConnectionResetError):pass
                return
            else:self.send_error(404);return
            self.respond(200,body,kind)
        def do_POST(self):
            path=urlsplit(self.path).path
            operations={'/api/distance/zero':feed.zero,'/api/distance/arm-zero':feed.arm_zero,
                        '/api/distance/cancel-zero':feed.cancel_zero}
            if path not in operations:return self.respond(404,{'error':'Not found'})
            if (not self.expected_host() or self.headers.get('Origin') not in
                (None,f'http://{host}:{port}',f'http://127.0.0.1:{port}',f'http://localhost:{port}') or
                not secrets.compare_digest(self.headers.get('X-Control-Token',''),token)):
                return self.respond(403,{'error':'Invalid measurement token/origin'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=128 or self.headers.get_content_type()!='application/json':
                    raise ValueError('Expected a small JSON object')
                if json.loads(self.rfile.read(length) or b'{}')!={}:raise ValueError('Measurement action expects an empty object')
                return self.respond(200,{'ok':True,'result':operations[path]()})
            except (ValueError,RuntimeError,OSError) as exc:
                return self.respond(409,{'ok':False,'error':str(exc)})
    return Handler


def start_video(host, port, root=ROOT):
    # Never take an existing camera bridge's ports or terminate its process.
    for address,video_port in ((host,13579),('127.0.0.1',13581)):
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as check:
            check.bind((address,video_port))
    return subprocess.Popen([sys.executable,'-u','-B',str(root/'vendor/xr_h2/head_camera/pico_video.py'),
        '--source',f'http://{host}:{port}/distance.jpg','--host',host,'--port','13579','--status-port','13581'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apk',type=Path,required=True)
    p.add_argument('--host',default=laptop_host())
    p.add_argument('--port',type=int,default=8027)
    p.add_argument('--pico-video',action='store_true')
    args=p.parse_args()
    from pico_distance_server import DistanceFeed
    feed=DistanceFeed()
    server=ThreadingHTTPServer((args.host,args.port),handler_for(args.apk,feed,host=args.host,port=args.port))
    server.daemon_threads=True
    video=None
    if args.pico_video:
        try:
            video=start_video(args.host,args.port)
            feed.set_video(process=video,enabled=True,detail=f'XRoboToolkit Remote Vision · {args.host} · ZEDMINI / H.264 / 2560×720')
        except OSError:
            feed.set_video(error='头显视频端口已被占用或不可用；未停止已有相机服务。')
    feed.start()
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(f'http://{args.host}:{args.port}/distance',flush=True)
    try:server.serve_forever(.2)
    finally:
        feed.close();server.server_close()
        if video and video.poll() is None:
            video.terminate()
            try:video.wait(timeout=3)
            except subprocess.TimeoutExpired:video.kill();video.wait()

if __name__=='__main__':main()
