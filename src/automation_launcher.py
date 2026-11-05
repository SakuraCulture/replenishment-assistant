"""Stable per-machine stdio launcher; no GUI, credentials, DB or platform access."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def resolve(path):
    data=json.loads(Path(path).read_text('utf-8'))
    if data.get('schema')!=1 or not all(isinstance(data.get(k),str) and data[k] for k in ('workspace','command','connection','application')):
        raise ValueError('本机登记无效，请从目标工作台配置/修复连接')
    if not Path(data['command']).is_absolute() or not Path(data['command']).is_file():
        raise ValueError('接口程序已移动或缺失，请打开新版补货助手并配置/修复连接')
    if not isinstance(data.get('args'),list) or any(not isinstance(a,str) for a in data['args']):raise ValueError('启动参数无效')
    if data['args'] and not Path(data['args'][0]).is_file():raise ValueError('源码接口已移动，请重新配置连接')
    return [data['command'],*data['args'],'--connection',data['connection'],'--workspace',data['workspace']]


def main(argv=None):
    parser=argparse.ArgumentParser(description='补货助手本机 AI 启动入口')
    parser.add_argument('--binding',type=Path,required=True)
    args,rest=parser.parse_known_args(argv)
    try:
        command=resolve(args.binding)
        # Inherited pipes preserve MCP framing. No shell expansion or hidden second service.
        return subprocess.call(command+rest,stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,ValueError,KeyError) as exc:
        message='AI 本机连接未启动：'+str(exc)+'；请在补货助手 AI 协助中修复。'
        if rest and rest[0] in ('doctor','call'):
            sys.stdout.buffer.write(json.dumps({'ok':False,'error':{'code':'REGISTRATION_INVALID','message':message}},ensure_ascii=False).encode('utf-8')+b'\n')
        else:sys.stderr.write(message+'\n')
        return 2


if __name__=='__main__':raise SystemExit(main())
