"""Console MCP stdio adapter. Does not import the GUI or access business databases."""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from automation_protocol import VERSION, MAX_FRAME, CommandError, decode, public_tools, validate_call


class Client:
    def __init__(self, connection): self.connection = Path(connection)

    def call(self, name, arguments):
        validate_call(name, arguments)
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QLocalSocket
        app = QCoreApplication.instance() or QCoreApplication([])
        try: settings = json.loads(self.connection.read_text('utf-8'))
        except (OSError, ValueError): raise CommandError('NOT_CONNECTED', '请先打开补货助手，在 AI 协助中启用本机连接') from None
        if settings.get('protocol') != VERSION: raise CommandError('VERSION_MISMATCH', '接口版本不兼容，请使用与程序一起交付的 AI 接口')
        socket = QLocalSocket(); socket.connectToServer(settings['server'])
        try:
            if not socket.waitForConnected(2000): raise CommandError('NOT_CONNECTED', '工作台连接不可用，请检查对应窗口是否已启用 AI 协助')
            rid = uuid.uuid4().hex
            payload = json.dumps({'id': rid, 'protocol': VERSION, 'token': settings['token'], 'action': name, 'arguments': arguments}, ensure_ascii=False, allow_nan=False).encode() + b'\n'
            if len(payload) > MAX_FRAME: raise CommandError('TOO_LARGE', '请求过大，请缩小门店或参数范围')
            socket.write(payload)
            if not socket.waitForBytesWritten(2000) and socket.bytesToWrite(): raise CommandError('CONNECTION_UNKNOWN', '发送状态不明，请先查询原任务，不要重复启动')
            data = bytearray(); deadline = time.monotonic() + 30
            while b'\n' not in data:
                if time.monotonic() >= deadline: raise CommandError('CONNECTION_UNKNOWN', '工作台响应超时；先查询原操作键或任务状态，不要重复提交')
                if socket.bytesAvailable() or socket.waitForReadyRead(500): data.extend(bytes(socket.readAll()))
                elif socket.state() == QLocalSocket.LocalSocketState.UnconnectedState: raise CommandError('CONNECTION_UNKNOWN', '工作台连接已断开；已受理任务不等于取消，请重连查询')
                if len(data) > MAX_FRAME: raise CommandError('TOO_LARGE', '结果过大，请分页读取')
            reply = decode(data.split(b'\n', 1)[0])
            if reply.get('id') != rid: raise CommandError('RESPONSE_MISMATCH', '响应不属于本次请求')
            if not reply.get('ok'):
                error = reply.get('error', {})
                raise CommandError(error.get('code', 'TASK_FAILED'), error.get('message', '操作未完成'), error.get('details'))
            return reply['result']
        finally: socket.abort()


def response(request, client, initialized):
    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    rid = request.get('id'); method = request.get('method')
    if rid is None: return None
    out = {'jsonrpc': '2.0', 'id': rid}
    if method == 'initialize':
        params = request.get('params') or {}; requested = params.get('protocolVersion')
        version = requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25') else '2025-11-25'
        initialized[0] = True
        return {**out, 'result': {'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'replenishment-workbench', 'version': '0.35.0'},
            'instructions': '先查询 workbench_context，明确门店编码、预案和文件后准备。长任务立即返回编号，用 task_status 查询；重复调用不新开任务。首期不签发、不采购、不删除。中文解释使用工具返回的业务参数，不猜缺失数据。'}}
    if method == 'ping': return {**out, 'result': {}}
    if not initialized[0]: return {**out, 'error': {'code': -32002, 'message': 'Initialize first'}}
    if method == 'tools/list': return {**out, 'result': {'tools': public_tools()}}
    if method == 'tools/call':
        params = request.get('params') or {}
        try:
            result = client.call(params.get('name'), params.get('arguments', {}))
            return {**out, 'result': {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, default=str)}], 'structuredContent': result, 'isError': False}}
        except CommandError as exc:
            error = {'code': exc.code, 'message': str(exc), 'details': exc.details}
        except Exception:
            error = {'code': 'BRIDGE_ERROR', 'message': '接口未完成请求，请检查连接后查询原任务'}
        return {**out, 'result': {'content': [{'type': 'text', 'text': json.dumps(error, ensure_ascii=False)}], 'structuredContent': error, 'isError': True}}
    return {**out, 'error': {'code': -32601, 'message': 'Method not found'}}


def main(argv=None):
    parser = argparse.ArgumentParser(description='补货助手本地 AI 接口（由 Codex / WorkBuddy 启动）')
    parser.add_argument('--connection', required=True, type=Path)
    args = parser.parse_args(argv); client = Client(args.connection); initialized = [False]
    # A separate console executable preserves inherited stdin/stdout in Windows.
    if sys.stdin is None or sys.stdout is None: raise RuntimeError('请使用 AI 接口控制台程序')
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line: break
        try: reply = response(decode(line), client, initialized)
        except CommandError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False, allow_nan=False, default=str).encode('utf-8') + b'\n'); sys.stdout.buffer.flush()
        if len(line) > MAX_FRAME: break
    return 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
