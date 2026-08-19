"""同步桥接客户端（从 asyncio 版本改写为 socket 同步）。

协议：
1. 连接后先发送 token + 换行
2. 再发送单行请求：
   - PING
   - EXEC|<wait_ms>|<command>
3. 服务端返回多行：
   - status=ok|error
   - code=...
   - output_b64=...
   - end=1
"""
import base64
import socket
from dataclasses import dataclass


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


class RconProtocolError(RconError):
    pass


@dataclass(frozen=True)
class RconConfig:
    host: str
    port: int
    password: str
    timeout: float = 5.0
    test_on_first_use: bool = True


class RconClient:
    """同步 socket 桥接客户端"""

    def __init__(self, cfg: RconConfig):
        self.cfg = cfg
        self._tested = False

    def close(self):
        """无状态连接，每次请求新建 socket，close 仅重置测试标记"""
        self._tested = False

    def _roundtrip(self, request_line: str) -> dict:
        """一次完整的连接-发送-接收-关闭流程"""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.cfg.timeout)
            sock.connect((self.cfg.host, self.cfg.port))

            # 发送 token + 请求
            sock.sendall((self.cfg.password + "\n").encode("utf-8"))
            sock.sendall((request_line + "\n").encode("utf-8"))

            # 读取多行响应
            buf = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                # 检查是否收到 end=1
                if b"end=1" in buf:
                    break

            result = {}
            for raw_line in buf.decode("utf-8", errors="replace").split("\n"):
                line = raw_line.rstrip("\r")
                if line == "end=1":
                    break
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key] = value
            return result

        except socket.timeout as e:
            raise RconError("Bridge read/write timeout") from e
        except ConnectionRefusedError as e:
            raise RconError(f"Bridge connection refused: {self.cfg.host}:{self.cfg.port}") from e
        except socket.error as e:
            raise RconError(f"Bridge socket error: {e}") from e
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    @staticmethod
    def _decode_output(result: dict) -> str:
        if "output_b64" in result:
            try:
                return base64.b64decode(result["output_b64"]).decode("utf-8", errors="replace")
            except Exception as e:
                raise RconProtocolError("Invalid output_b64 payload") from e
        if "output" in result:
            try:
                return base64.b64decode(result["output"]).decode("utf-8", errors="replace")
            except Exception:
                return result["output"]
        return ""

    def auth(self):
        """PING 测试认证"""
        data = self._roundtrip("PING")
        if data.get("status") != "ok":
            code = str(data.get("code", ""))
            if code == "AUTH_FAILED":
                raise RconAuthError("Bridge auth failed")
            raise RconError(code or "Bridge ping failed")
        self._tested = True

    def ensure_ready(self):
        """首次使用前做一次 PING 测试"""
        if self.cfg.test_on_first_use and not self._tested:
            self.auth()

    def exec(self, command: str, wait_ms: int = 0) -> str:
        """执行命令，返回输出文本"""
        self.ensure_ready()
        data = self._roundtrip(f"EXEC|{max(0, int(wait_ms))}|{command}")
        if data.get("status") != "ok":
            code = str(data.get("code", ""))
            if code == "AUTH_FAILED":
                raise RconAuthError("Bridge auth failed")
            raise RconError(code or "Bridge exec failed")
        return self._decode_output(data).strip("\n")
