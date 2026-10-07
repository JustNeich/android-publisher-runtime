import json,tempfile,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from publisher.phone import Phone,endpoint
from publisher.config import UserError
from urllib.request import urlopen
from urllib.error import HTTPError
from pathlib import Path

class Transport(unittest.TestCase):
 def test_private_endpoint_only(self):
  for value in ('https://8.8.8.8/mcp','http://127.0.0.1:80/mcp','http://192.168.1.2:80/mcp?token=abc','http://user:secret@192.168.1.2:80/mcp'):
   with self.assertRaises(UserError):endpoint(value)
 def test_mcp_notification_null(self):
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_POST(self):
    request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
    payload=b'null' if 'id' not in request else json.dumps({'id':request['id'],'jsonrpc':'2.0','result':{'protocolVersion':'2025-03-26'}}).encode()
    self.send_response(200);self.send_header('Mcp-Session-Id','test-session');self.end_headers();self.wfile.write(payload)
   def do_DELETE(self):self.send_response(200);self.end_headers()
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
  try:
   with patch('publisher.phone.endpoint',side_effect=lambda x:x):
    with Phone('http://127.0.0.1:'+str(server.server_port)+'/mcp','test-token') as phone:self.assertEqual(phone.session,'test-session')
  finally:server.shutdown();server.server_close();worker.join()

if __name__=='__main__':unittest.main()
