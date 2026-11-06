"""Console MCP stdio adapter. Does not import the GUI or access business databases."""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from automation_protocol import VERSION, MAX_FRAME, CommandError, decode, public_tools, validate_call, wire_default
from automation_connection import load_connection
from version_info import diagnostic


def version_details():
    info = diagnostic()
    return {k: info[k] for k in ('app', 'version', 'model_version', 'routing_version',
            'parameter_schema_version', 'build_id', 'source_commit') if k in info} | {'protocol': VERSION}


class Client:
    def __init__(self, connection, workspace=None):
        self.connection = Path(connection);self.workspace=workspace;self.client_name='CLI'

    def call(self, name, arguments):
        validate_call(name, arguments)
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QLocalSocket
        app = QCoreApplication.instance() or QCoreApplication([])
        settings = load_connection(self.connection,self.workspace)
        if settings.get('protocol') != VERSION: raise CommandError('VERSION_MISMATCH', '接口版本不兼容，请使用与程序一起交付的 AI 接口')
        socket = QLocalSocket(); socket.connectToServer(settings['server'])
        try:
            if not socket.waitForConnected(2000): raise CommandError('NOT_CONNECTED', '工作台连接不可用，请检查对应窗口是否已启用 AI 协助')
            rid = uuid.uuid4().hex
            payload = json.dumps({'id': rid, 'protocol': VERSION, 'token': settings['token'], 'action': name, 'arguments': arguments,
                'bridge_version':version_details()['version'],'workspace':self.workspace,'client_name':self.client_name}, ensure_ascii=False, allow_nan=False).encode() + b'\n'
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
        client.client_name='MCP / '+str((params.get('clientInfo') or {}).get('name','未提供客户端名'))[:100]
        return {**out, 'result': {'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'replenishment-workbench', 'version': version_details()['version']},
            'instructions': '先查询 workbench_context 核对实例、资料环境和作业。异步任务逐个完成后再开始下一任务，task_status用after续查，不密集轮询。预计可售天数读取coverage，固定制单时预测；按货值加权，跨轮只取标明日期的最新轮，实收新增天数不是当前库存；query.coverage_through选择共同可比截止环节，rounds逐轮读取。经营漏斗query_order_funnel→task_status→order_funnel_results分页，export_order_funnel导出。多轮store_history_review中day是本轮观察日期，evidence_details.business_date/order_number是证据所属日期/单号，不能归到本轮；parameter_basis为当时参数，当前参数不同不是历史错误。库存诊断start_stock_diagnosis→task_status→stock_diagnosis_results；完整候选参数用stock_diagnosis_parameters(task_id,candidate,stores)，不是effective_parameters的preview会话。risk_summary分别说明原有/候选缺口，risk_count只指新增或加剧风险；recommended不表示无缺货风险。0和未导入/待核不同，回传不是退货。读guidance.definitions核对参数约束，理论模型减少不等于实际节省；人员明确候选及原因后才adopt_stock_diagnosis。定时只读；不签发、不采购、不删除。'}}
    if method == 'ping': return {**out, 'result': {}}
    if not initialized[0]: return {**out, 'error': {'code': -32002, 'message': 'Initialize first'}}
    if method == 'tools/list': return {**out, 'result': {'tools': public_tools()}}
    if method == 'tools/call':
        params = request.get('params') or {}
        try:
            result = client.call(params.get('name'), params.get('arguments', {}))
            return {**out, 'result': {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, default=wire_default)}], 'structuredContent': result, 'isError': False}}
        except CommandError as exc:
            error = {'code': exc.code, 'message': str(exc), 'details': exc.details}
        except Exception:
            error = {'code': 'BRIDGE_ERROR', 'message': '接口未完成请求，请检查连接后查询原任务'}
        return {**out, 'result': {'content': [{'type': 'text', 'text': json.dumps(error, ensure_ascii=False)}], 'structuredContent': error, 'isError': True}}
    return {**out, 'error': {'code': -32601, 'message': 'Method not found'}}


def cli(args):
    """Same named-pipe contract as MCP. No business DB access or implicit retries."""
    try:
        if args.command == 'version': result = version_details()
        elif args.command == 'tools': result = {'protocol': VERSION, 'tools': public_tools()}
        elif args.command == 'doctor':
            context = Client(args.connection,getattr(args,'workspace',None)).call('workbench_context', {})
            result = {'bridge': version_details(), 'workbench': context,
                      'versions_match': context['application'] == version_details()['version'],
                      'note': '连接诊断只查询；请关闭业务弹窗后再调用其他查询。'}
        else:
            raw = args.arguments or '{}'
            if args.arguments_file:
                with args.arguments_file.open('rb') as f: raw = f.read(MAX_FRAME + 1)
                raw = raw.decode('utf-8-sig')
            arguments = decode(raw.encode('utf-8'))
            result = Client(args.connection,getattr(args,'workspace',None)).call(args.tool, arguments)
        value, code = {'ok': True, 'result': result}, 0
    except CommandError as exc:
        value, code = {'ok': False, 'error': {'code': exc.code, 'message': str(exc), 'details': exc.details}}, 2
    except (OSError, UnicodeError):
        value, code = {'ok': False, 'error': {'code': 'INPUT_ERROR', 'message': '无法读取 UTF-8 参数文件，请检查文件和权限'}}, 2
    except Exception:
        value, code = {'ok': False, 'error': {'code': 'BRIDGE_ERROR', 'message': '接口未完成请求，请检查连接后查询原任务'}}, 3
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False, allow_nan=False, default=wire_default).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description='补货助手 AI / CLI：无子命令时保持 MCP stdio；所有业务调用须连接已启用的工作台')
    parser.add_argument('--connection', type=Path, help='工作台生成的 connection.json；放在子命令前')
    parser.add_argument('--workspace', help='明确绑定的资料环境；不一致拒绝连接，不自动切换')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('version', help='离线查看接口版本和构建提交')
    commands.add_parser('tools', help='离线查看开放工具及参数契约')
    commands.add_parser('doctor', help='只读检查本机连接和工作台版本')
    call = commands.add_parser('call', help='调用开放工具，不直接访问业务数据库')
    call.add_argument('tool')
    parameters = call.add_mutually_exclusive_group()
    parameters.add_argument('--arguments', help='JSON 对象，默认 {}')
    parameters.add_argument('--arguments-file', type=Path, help='UTF-8 JSON 文件，避免命令行引号问题')
    args = parser.parse_args(argv)
    if args.command not in ('version', 'tools') and args.connection is None:
        parser.error('MCP / doctor / call 需要 --connection；请从软件 AI 协助生成连接配置')
    if args.command: return cli(args)
    client = Client(args.connection,args.workspace); initialized = [False]
    # A separate console executable preserves inherited stdin/stdout in Windows.
    if sys.stdin is None or sys.stdout is None: raise RuntimeError('请使用 AI 接口控制台程序')
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line: break
        try: reply = response(decode(line), client, initialized)
        except CommandError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False, allow_nan=False, default=wire_default).encode('utf-8') + b'\n'); sys.stdout.buffer.flush()
        if len(line) > MAX_FRAME: break
    return 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
