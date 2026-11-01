
import sys
import json
import time
import socket
import subprocess
import urllib.request

from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8765
TTL = 300  # 5 minut

IPS = ["100.88.126.40", "100.72.44.49"]

my_ip = subprocess.check_output(
    ["tailscale", "ip", "-4"], text=True
).strip()

if my_ip not in IPS:
    sys.exit("Nieznany adres Tailscale")

other_ip = next(ip for ip in IPS if ip != my_ip)

messages = []


def active_messages():
    """Usuwa wiadomości starsze niż 5 minut."""
    messages[:] = [
        msg for msg in messages
        if time.time() - msg["time"] < TTL
    ]
    return messages


class Handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        data = json.loads(self.rfile.read(length))

        active_messages()
        messages.append({
            "device": data["device"],
            "message": data["message"],
            "time": time.time()
        })

        print(f'\n[{data["device"]}] {data["message"]}', flush=True)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        """Udostępnia wszystkie aktywne wiadomości."""
        data = [
            {
                "device": msg["device"],
                "message": msg["message"]
            }
            for msg in active_messages()
        ]

        body = json.dumps(data, ensure_ascii=False).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def request(ip, data=None):
    url = f"http://{ip}:{PORT}"
    if data is not None:
        data = json.dumps(data).encode()

    with urllib.request.urlopen(url, data=data, timeout=5) as response:
        return response.read().decode()


if len(sys.argv) > 1:

    if sys.argv[1] == "--read":
        # Odczytaj wiadomości odebrane na tym komputerze
        print(request(my_ip))

    else:
        # Wyślij wiadomość do drugiego komputera
        print(request(other_ip, {
            "device": socket.gethostname(),
            "message": " ".join(sys.argv[1:])
        }))

else:
    print(f"Nasłuchiwanie: http://{my_ip}:{PORT}")
    HTTPServer((my_ip, PORT), Handler).serve_forever()
