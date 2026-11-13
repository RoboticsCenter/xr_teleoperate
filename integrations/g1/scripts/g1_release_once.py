#!/usr/bin/env python3
"""One explicit official ReleaseMode call, with before/after CheckMode evidence.

This changes the G1 motion-service state. It sends no joint targets, does not
restore a mode, and is intended only for the confirmed suspended commissioning
session. Without --release it only prints a plan and imports no robot SDK.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

from g1_mode_probe import AUDITED_FILES


def run(interface):
    out=os.fdopen(os.dup(1),'w',buffering=1)
    os.dup2(2,1)
    report={'type':'release_result','started_at_utc':datetime.now(timezone.utc).isoformat(),
            'rpc_calls':[],'motor_commands_published':0,'mode_restored':False}
    timer=threading.Timer(12,lambda:os._exit(3)); timer.daemon=True; timer.start()
    client=None
    try:
        import unitree_sdk2py.comm.motion_switcher.motion_switcher_client as module
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        root=Path(module.__file__).resolve().parents[2]
        actual={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in AUDITED_FILES}
        if actual!=AUDITED_FILES:
            raise RuntimeError('SDK implementation differs from reviewed version')
        report['sdk_source_sha256']=actual
        ChannelFactoryInitialize(0,interface)
        class Once(module.MotionSwitcherClient):
            def _Call(self, api, parameter):
                expected=[1001,1003,1001]
                index=len(report['rpc_calls'])
                if index>=len(expected) or api!=expected[index] or json.loads(parameter)!={}:
                    raise RuntimeError('unexpected RPC or repeated release blocked')
                report['rpc_calls'].append({'api_id':api,'parameter':parameter,
                                            'time_utc':datetime.now(timezone.utc).isoformat()})
                return super()._Call(api,parameter)
        client=Once(); client.SetTimeout(2); client.Init()
        code,before=client.CheckMode()
        report['before']={'code':code,'data':before}
        if code!=0 or not isinstance(before,dict) or before.get('name') not in ('','ai'):
            raise RuntimeError('current motion service is not the reviewed ai state')
        if before['name']=='':
            report['release_sent']=False
            report['after']=report['before']
        else:
            report['release_sent']=True
            release_code,_=client.ReleaseMode()
            report['release_code']=release_code
            time.sleep(1)
            code,after=client.CheckMode()
            report['after']={'code':code,'data':after}
        after=report['after']
        report['service_inactive_verified']=(after['code']==0 and isinstance(after['data'],dict)
                                             and after['data'].get('name')=='')
        if report.get('release_sent') and report.get('release_code') != 0:
            raise RuntimeError('ReleaseMode returned nonzero; preserve later query but do not report action success')
        if not report['service_inactive_verified']:
            raise RuntimeError('motion service release not verified; no automatic retry')
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        if client is not None:
            try:
                stub=getattr(client,'_ClientBase__stub')
                getattr(stub,'_ClientStub__recvChannel').CloseReader()
                getattr(stub,'_ClientStub__sendChannel').CloseWriter()
            except Exception as exc:
                report['cleanup_error']=str(exc)
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat()
        print(json.dumps(report,allow_nan=False),file=out,flush=True)
        timer.cancel()
    return 1 if report.get('error') or report.get('cleanup_error') else 0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interface',default='eth0')
    p.add_argument('--release',action='store_true')
    args=p.parse_args()
    if not args.release:
        print(json.dumps({'plan_only':True,'sequence':['CheckMode','ReleaseMode once if ai','CheckMode'],
                          'changes_motion_service':True,'motor_commands_published':0,'no_automatic_retry':True}))
        return 0
    return run(args.interface)


if __name__=='__main__':
    raise SystemExit(main())
