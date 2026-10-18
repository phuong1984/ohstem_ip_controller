import network
import socket
import time

from utility import translate

# IP tinh cua robot, phai khop voi ROBOT_IP trong file html dieu khien
STATIC_IP = "192.168.1.50"
SUBNET = "255.255.255.0"
GATEWAY = "192.168.1.1"
DNS = "192.168.1.1"
PORT = 80

# Ten nut gui tu trang dieu khien
BTN_DPAD_UP = "dpad_up"
BTN_DPAD_DOWN = "dpad_down"
BTN_DPAD_LEFT = "dpad_left"
BTN_DPAD_RIGHT = "dpad_right"
BTN_TRIANGLE = "triangle"
BTN_SQUARE = "square"
BTN_CIRCLE = "circle"
BTN_CROSS = "cross"
BTN_L1 = "l1"
BTN_L2 = "l2"
BTN_R1 = "r1"
BTN_R2 = "r2"
BTN_SHARE = "share"
BTN_OPTIONS = "options"

BUTTONS = (
    BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT,
    BTN_TRIANGLE, BTN_SQUARE, BTN_CIRCLE, BTN_CROSS,
    BTN_L1, BTN_L2, BTN_R1, BTN_R2, BTN_SHARE, BTN_OPTIONS,
)

STATE_DOWN = "down"
STATE_UP = "up"

JOY_LEFT = "left"
JOY_RIGHT = "right"

RECV_SIZE = 512
# Cho phep doc mot request cham mot chut. Sau thoi gian nay ma chua co gi
# thi ket noi do la rac (trinh duyet mo socket truoc khi gui) va bo qua.
RECV_TIMEOUT = 0.2

# Cho phep cho byte dau tien cua request. Request that tu may khach
# da co san trong bo dem ngay khi accept() tra ve, nen 50ms la du rong.
# Ket noi rac (trinh duyet mo socket truoc khi gui) se bi bo sau 50ms
# thay vi 200ms - so lan ket noi rac moi giay quyet dinh robot co
# kip hay khong.
FIRST_BYTE_TIMEOUT = 0.05

# So ket noi cho san trong hang doi cua cong. Server tra
# 'Connection: close' nen moi lenh la mot ket moi; khi tran so nay
# he dieu hanh vut SYN va may khach phai gui lai sau 1s, 2s, 4s -
# nguyen do cua do tre vai giay.
LISTEN_BACKLOG = 16

# So request toi da doc trong mot lan goi process_all(). Tran dieu khien
# gui joystick lien tuc, nen phai co tran de mot vong lap khong bi ket.
MAX_REQUESTS_PER_LOOP = 10

# Gioi han hang doi de bao ve RAM. Khi day, server tra 503 de trinh duyet
# thu lai lenh cu thay vi am tham bo mat lenh.
PENDING_LIMIT = 32


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def to_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def parse_query(raw_path):
    params = {}
    if "?" not in raw_path:
        return raw_path, params

    base, query = raw_path.split("?", 1)
    for pair in query.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            params[key.strip()] = value.strip()
    return base, params


def send_response(conn, body="OK", status=200):
    reason = "OK" if status == 200 else "Service Unavailable"
    conn.send((
        "HTTP/1.1 {} {}\r\n".format(status, reason) +
        "Access-Control-Allow-Origin: *\r\n"
        "Content-Type: text/plain\r\n"
        "Connection: close\r\n\r\n" + body
    ).encode())


class IPController:
    """Web server dieu khien robot qua mang.

    Server khong chay nen. main.py phai goi process() lap lai de xu ly
    tung request mot, vi du trong khoi 'lap vo han'.
    """

    def __init__(self):
        self.wlan = None
        self.sock = None
        self.signal = None
        self.value = {}
        # Hang doi tin hieu chua kip xu ly. process_all() day vao day,
        # has_signal()/clear_signal() rut ra tu dung thu tu.
        self.pending = []
        self.last_seq = {}  # Lưu seq lớn nhất đã nhận của từng joystick
        self.last_button_seq = -1
        self.joystick_received = 0
        self.joystick_accepted = 0
        self.joystick_processed = 0
        self._joystick_stats_tick = self._ticks_ms()
        self._joystick_stats_snapshot = (0, 0, 0)
        self._joystick_stats_due = False
        self._joystick_stats_elapsed = 0

    def _ticks_ms(self):
        ticks_ms = getattr(time, "ticks_ms", None)
        if ticks_ms is not None:
            return ticks_ms()
        return int(time.time() * 1000)

    def _ticks_diff(self, newer, older):
        ticks_diff = getattr(time, "ticks_diff", None)
        if ticks_diff is not None:
            return ticks_diff(newer, older)
        return newer - older

    def _report_joystick_stats(self):
        now = self._ticks_ms()
        current = (self.joystick_received, self.joystick_accepted,
                   self.joystick_processed)
        if self._joystick_stats_due:
            previous = self._joystick_stats_snapshot
            if current != previous:
               """  print("joy/{}ms rx={} accepted={} processed={}".format(
                    self._joystick_stats_elapsed, current[0] - previous[0],
                    current[1] - previous[1], current[2] - previous[2])) """
            self._joystick_stats_snapshot = current
            self._joystick_stats_due = False

        elapsed = self._ticks_diff(now, self._joystick_stats_tick)
        if elapsed >= 1000:
            self._joystick_stats_elapsed = elapsed
            self._joystick_stats_tick = now
            self._joystick_stats_due = True

    def _reset_joystick_stats(self):
        self.joystick_received = 0
        self.joystick_accepted = 0
        self.joystick_processed = 0
        self._joystick_stats_snapshot = (0, 0, 0)
        self._joystick_stats_tick = self._ticks_ms()
        self._joystick_stats_due = False
        self._joystick_stats_elapsed = 0

    # ---------------- WIFI ----------------

    def connect_wifi(self, ssid, password, timeout=20):
        if not ssid:
            print("Chua nhap ten wifi. Dien ten vao block 'ket noi wifi'.")
            return False

        if self.wlan is None:
            self.wlan = network.WLAN(network.STA_IF)
            self.wlan.active(True)
            self.wlan.ifconfig((STATIC_IP, SUBNET, GATEWAY, DNS))

        if not self.wlan.isconnected():
            print("Dang ket noi wifi", ssid, "...")
            self.wlan.connect(ssid, password)
            while not self.wlan.isconnected() and timeout > 0:
                time.sleep(1)
                timeout -= 1
                print(".", end="")

        if self.wlan.isconnected():
            print("\nKet noi wifi thanh cong. Dia chi IP:", self.get_ip())
            return True

        print("\nKhong ket noi duoc wifi. Kiem tra lai ten va mat khau.")
        return False

    def is_connected(self):
        if self.wlan is None:
            return False
        return self.wlan.isconnected()

    def get_ip(self):
        if self.wlan is None:
            return ""
        return self.wlan.ifconfig()[0]

    # ---------------- SERVER ----------------

    def start_server(self):
        """Mo cong cho trang dieu khien. Tra ve True neu thanh cong."""
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

        server_sock = None
        try:
            addr = socket.getaddrinfo("0.0.0.0", PORT)[0][-1]
            server_sock = socket.socket()
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind(addr)
            server_sock.listen(LISTEN_BACKLOG)
            server_sock.settimeout(0)
        except OSError as error:
            if server_sock is not None:
                try:
                    server_sock.close()
                except Exception:
                    pass
            self.sock = None
            print("Khong mo duoc cong", PORT, "-", error)
            print("Cong dang bi chiem. Hay reset robot roi chay lai,")
            print("May chu dieu khien su dung co dinh cong", PORT, ".")
            return False

        self.sock = server_sock
        self._reset_joystick_stats()
        print("May chu dieu khien da mo, cong", PORT)
        return True

    def process(self):
        """Xu ly toi da mot request. Tra ve True neu co request that su."""
        # Xem ghi chu trong process_all() ve viec bo tin hieu con lai.
        self._clear_signal()

        accepted, dispatched = self._serve_one()
        if not accepted:
            return False
        self._promote()
        return dispatched

    def process_all(self, max_count=MAX_REQUESTS_PER_LOOP):
        """Doc het cac request dang cho san vao bo dem noi bo.

        Khong chan: chi doc nhung request da co san. Ket noi rac bi bo qua
        nhung van tinh vao so da dem, nen mot ket noi rac khong lam dung
        phan con lai cua hang doi.

        Su kien duoc giu theo FIFO. Moi vong lap promote mot tin hieu de cac
        khoi su kien kiem tra va xu ly cung mot tin hieu do.
        """
        # Tin hieu con lai tu luot truoc la tin hieu khong khop khoi su kien
        # nao, nen bo di. Giu lai no se chan cua hang doi va lam robot
        # dung im cho den khi co lenh moi den.
        self._clear_signal()

        served = 0
        while served < max_count:
            accepted, _ = self._serve_one()
            if not accepted:
                break
            served += 1
        self._promote()
        self._report_joystick_stats()
        return served

    def _serve_one(self):
        """Nhan va xu ly MOT ket noi. Khong cho.

        Tra ve cap (nhan, xu_ly):
          nhan   - True neu co bat ky ket noi nao da duoc nhan, ke ca ket
                   noi rac. False neu hang doi rong.
          xu_ly  - True neu ket noi do co request that su da duoc xu ly.
        """
        if self.sock is None:
            return False, False

        try:
            conn, _ = self.sock.accept()
        except OSError:
            return False, False

        dispatched = False
        try:
            conn.settimeout(FIRST_BYTE_TIMEOUT)
            request = self._read_request(conn)
            if request:
                self._dispatch(conn, request)
                dispatched = True
        except OSError:
            # Client da ket noi roi bien mat giua chung, hoac tra loi khi ta gui
            # ket qua. Day la viec cap mang, khong phai loi robot va khong can
            # bao loi. Ket noi da duoc dong o finally.
            pass
        except Exception as error:
            print("Loi xu ly request:", error)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return True, dispatched

    def _read_request(self, conn):
        """Tra ve request da doc duoc.

        Time out giua chung van tra ve phan da nhan, vi du dong 'GET /ping
        HTTP/1.1' da toi nhung chua het header. Dong request line la du de
        xu ly, nen tra ve thay vi nem loi.
        """
        request = ""
        waiting_first_byte = True
        while "\r\n\r\n" not in request and len(request) < RECV_SIZE:
            try:
                chunk = conn.recv(RECV_SIZE)
            except OSError:
                break
            if not chunk:
                break
            request += chunk.decode()
            if waiting_first_byte:
                # Da co byte dau tien. Phan header con lai co lien quan
                # toi request that nen cho thong thoai hon.
                waiting_first_byte = False
                conn.settimeout(RECV_TIMEOUT)
        return request

    def _dispatch(self, conn, request):
        if not request:
            send_response(conn, "bad request")
            return

        parts = request.split("\r\n")[0].split(" ")
        path, params = parse_query(parts[1] if len(parts) > 1 else "/")

        if path == "/button":
            name = params.get("name", "")
            state = params.get("state", "")
            seq = to_int(params.get("seq"), 0)
            if seq > 0 and seq <= self.last_button_seq:
                send_response(conn, "duplicate")
                return
            if name not in BUTTONS:
                print("Ten nut la:", repr(name))
            # down/up la cac su kien doc lap, khong duoc gop mat nhau.
            if not self._queue_signal("btn:{}:{}".format(name, state), {"state": state}):
                send_response(conn, "busy", status=503)
                return
            if seq > 0:
                self.last_button_seq = seq
            send_response(conn, "button:{}:{}".format(name, state))

        elif path == "/joystick":
            self.joystick_received += 1
            joy_id = params.get("id", JOY_LEFT)
            x = clamp(to_int(params.get("x")), -100, 100)
            y = clamp(to_int(params.get("y")), -100, 100)
            seq = to_int(params.get("seq"), 0)  # Lấy seq từ URL query

            # Bo qua retry da duoc xep hang thanh cong truoc do.
            if seq > self.last_seq.get(joy_id, -1) or seq == 0:
                queued = self._queue_signal("joy:{}".format(joy_id), {"x": x, "y": y})
                if not queued:
                    send_response(conn, "busy", status=503)
                    return
                self.last_seq[joy_id] = seq
                self.joystick_accepted += 1

            send_response(conn, "joystick:{}:{}:{}".format(joy_id, x, y))

        elif path == "/ping":
            self._queue_signal("ping", {})
            send_response(conn, "pong")

        else:
            send_response(conn, "unknown path")

    def _set_signal(self, signal, value):
        self.signal = signal
        self.value = value

    def _queue_signal(self, signal, value, family=None):
        """Giu tin hieu theo thu tu den, khong vuot gioi han bo dem."""
        if family is not None:
            self.pending = [item for item in self.pending if item[2] != family]
        if len(self.pending) >= PENDING_LIMIT:
            return False
        self.pending.append((signal, value, family))
        return True

    def _promote(self):
        """Dua tin hieu dau tien trong bo dem ra self.signal de doc."""
        if self.pending:
            signal, value, _family = self.pending.pop(0)
            self._set_signal(signal, value)

    # ---------------- TIN HIEU ----------------

    def has_signal(self):
        # Chi kiem tra signal hien tai; process_all() promote moi signal mot lan.
        return self.signal is not None

    def signal_name(self):
        if self.signal is None:
            return ""
        return self.signal

    def signal_state(self):
        return self.value.get("state", "")

    def signal_x(self):
        return self.value.get("x", 0)

    def signal_y(self):
        return self.value.get("y", 0)

    def clear_signal(self):
        if self.signal is not None and self.signal.startswith("joy:"):
            self.joystick_processed += 1
        self._clear_signal()

    def _clear_signal(self):
        self.signal = None
        self.value = {}


ip_ctrl = IPController()
