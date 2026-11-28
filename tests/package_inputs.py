"""Exercise documented input contracts with inert text on packaged entry points."""
import argparse
import csv
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tarfile
import tempfile

parser=argparse.ArgumentParser()
parser.add_argument('package',type=Path)
parser.add_argument('output',type=Path)
args=parser.parse_args()
out=args.output.resolve()
out.mkdir(exist_ok=False)
digest=hashlib.sha256(args.package.read_bytes()).hexdigest()
rows=[]
text='Café & "June" (Δ), 7'
class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.words=[]
    def handle_data(self,data):
        self.words.append(data)

with tempfile.TemporaryDirectory() as tmp:
    base=Path(tmp)
    with tarfile.open(args.package) as archive:
        for item in archive:
            p=Path(item.name)
            if item.isdir() and not p.is_absolute() and '..' not in p.parts and item.mode == 0o755:
                continue
            if not item.isfile() or p.is_absolute() or '..' in p.parts or len(p.parts)<2:
                raise ValueError('unexpected archive item')
            dest=base/'package'/Path(*p.parts[1:])
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(archive.extractfile(item).read())
            dest.chmod(item.mode&0o755)
    work=base/'inputs';work.mkdir()
    cfg=work/(text+'.json');key=secrets.token_hex(6)+' Ŕ&'
    cfg.write_text(json.dumps({'key':key},ensure_ascii=False));cfg.chmod(0o600)
    env=dict(os.environ,EDGEWALKER_CONFIG=str(cfg),HOME=str(base/'home'))
    env.pop('EDGEWALKER_ENGINE',None)
    bins={'launcher':base/'package/edgewalker','native':base/'package/bin/edgewalker-native'}
    def call(program,command,argv,stdin=None):
        return subprocess.run([bins[program]]+([command] if command else [])+list(map(str,argv)),
                              capture_output=True,text=True,input=stdin,cwd=work,env=env,timeout=7)
    def record(program,command,surface,case,expected,actual,ok):
        rows.append(dict(program=program,command=command or 'root',surface=surface,case=case,expected=expected,
                         actual=json.dumps(actual,sort_keys=True),status='PASS' if ok else 'FAIL'))
    def reject(program,command,flag,argv,case='invalid value',no_files=True):
        before=set(work.iterdir())
        r=call(program,command,argv)
        clean=key not in (r.stdout+r.stderr)
        trace='Traceback' in r.stderr
        effects=sorted(x.name for x in set(work.iterdir())-before)
        record(program,command,flag,case,'Nonzero exit, secret-safe diagnostic, no traceback'+(', no created paths' if no_files else ''),
               dict(exit=r.returncode,diagnostic=bool(r.stderr),secret_absent=clean,traceback=trace,created_count=len(effects)),
               r.returncode!=0 and clean and not trace and bool(r.stderr) and (not no_files or not effects))
    linked = base / "linked launcher"
    linked.symlink_to(os.path.relpath(bins['launcher'],base))
    version=subprocess.run([linked,'--version'],capture_output=True,text=True,env=env)
    record('launcher','root','--version','relative symlink','Report version through a PATH link',
           dict(exit=version.returncode),version.returncode==0 and version.stdout.startswith('edgewalker '))
    for pr,commands in [('launcher',['','run','server','keygen','report','replay']),('native',['','client','server','keygen','help'])]:
        for cmd in commands:
            reject(pr,cmd,'command',['--unlisted-setting',text],case='unknown option')
        reject(pr,'','command',[text],case='unknown literal subcommand')
    r=call('native','help',['server'])
    record('native','help','command','known help topic','Displays server usage',dict(exit=r.returncode),r.returncode==0 and '--listen' in r.stdout)
    reject('native','help','topic',[text],case='unknown help topic')
    noaddr='127.0.0.1:0'
    basic=['--connect',noaddr,'--mode','udp','--forward-max','400000']
    launch=basic+['--output',work/'invalid','--duration','1']
    server=['--listen',noaddr,'--capacity','400000']
    for pr,cmd,common,values in [
        ('launcher','run',launch,['--connect','--mode','--duration','--window-ms','--udp-size','--overload-ms','--timing-every','--tcp-send-buffer','--flow-limit','--flows','--telemetry-interval','--forward-max','--return-max','--forward-start','--return-start']),
        ('native','client',basic,['--connect','--mode','--forward-max','--return-max','--udp-size','--overload-ms','--timing-every','--tcp-send-buffer','--flow-limit']),
        ('launcher','server',server,['--listen','--data-ports','--capacity','--allow-overload-ms','--tx','--rx']),
        ('native','server',server,['--listen','--data-ports','--capacity','--allow-overload-ms','--tx','--rx'])]:
        for flag in values:
            tokens=list(common)
            if flag in tokens:
                index=tokens.index(flag)
                del tokens[index:index+2]
            reject(pr,cmd,flag,tokens+[flag,text],no_files=(pr,flag)!=('launcher','--connect'))
    for pr in ('launcher','native'):
        reject(pr,'server','--capacity/--tx/--rx',server+['--tx','12','--rx','12'],case='conflicting settings')
        for mode in ('keygen','server'):
            reject(pr,mode,'--key',['--key'],case='missing value')
        for flag,value in [('--config',work/'unused'),('--key',key)]:
            r=call(pr,'keygen',[flag,value])
            record(pr,'keygen',flag,'incompatible','Reject without disclosing a key',dict(exit=r.returncode),
                   r.returncode!=0 and not r.stdout and key not in r.stderr)
        dest=work/(pr+' private.json')
        r=call(pr,'keygen',['--output',dest])
        original=dest.read_bytes()
        retry=call(pr,'keygen',['--output',dest])
        record(pr,'keygen','--output','exclusive private file','Create once at mode 0600',
               dict(exit=r.returncode,retry=retry.returncode),r.returncode==0 and not r.stdout
               and dest.stat().st_mode & 0o777 == 0o600 and len(json.loads(original)['key'])==16
               and retry.returncode!=0 and not retry.stdout and dest.read_bytes()==original)
    for pr,cmd,common in [('launcher','server',server),('native','server',server),('native','client',basic)]:
        reject(pr,cmd,'--config',common+['--config',text],case='literal missing path')
        reject(pr,cmd,'--key',common+['--key','tiny'],case='invalid key length')
    for pr,cmd in [('launcher','server'),('native','server'),('native','client')]:
        common=server if cmd=='server' else basic
        before=set(work.iterdir());badsecret=secrets.token_hex(3)
        r=call(pr,cmd,common+['--key',badsecret])
        record(pr,cmd,'--key','redacted invalid credential','Length diagnostic without echoing credential',
               dict(exit=r.returncode,secret_absent=badsecret not in r.stdout+r.stderr,created_count=len(set(work.iterdir())-before)),
               r.returncode!=0 and badsecret not in r.stdout+r.stderr and before==set(work.iterdir()))
    badcfg=work/'malformed-config'
    badcfg.write_text('{"key":')
    reject('native','server','--config',server+['--config',badcfg],case='invalid config JSON')
    for kind,data in [('syntax','{'),('shape','[]'),('range','{"forward":{"ceiling_bps":-5}}')]:
        path=work/('bad-plan-'+kind);path.write_text(data)
        reject('launcher','run','--plan',launch+['--plan',path],case='invalid plan '+kind)
    reject('launcher','run','--manual-resume',launch+['--manual-resume='+text],case='boolean with literal argument')
    for cmd in ('report','replay'):
        reject('launcher',cmd,'path',[text],case='missing literal input path')
    for kind,value in [('syntax','{'),('wrong-root','[]'),('wrong-events','{"session":"x","directions":{"forward":{}},"events":1}')]:
        p=work/('trace-'+kind);p.write_text(value)
        reject('launcher','replay','path',[p],case='malformed trace '+kind)
    trace=work/(text+' trace')
    trace.write_bytes((base/'package/examples/udp-replay.json').read_bytes())
    one=call('launcher','replay',[trace]);two=call('launcher','replay',[trace])
    record('launcher','replay','path','literal name','Identical successful replay output twice',dict(bytes=len(one.stdout),exit=one.returncode),one.returncode==two.returncode==0 and bool(one.stdout) and one.stdout==two.stdout)
    bad=work/'bad-session';bad.mkdir()
    (bad/'session.json').write_text('[]')
    reject('launcher','report','path',[bad],case='malformed session manifest')
    r=subprocess.Popen([bins['launcher'],'server','--listen',noaddr,'--capacity','1000000','--config',cfg],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        addr=json.loads(r.stdout.readline())['listening']
        plan=work/(text+' plan.json')
        plan.write_text(json.dumps({'forward':{'start_bps':320000,'ceiling_bps':320000}}))
        session=work/(text+' results')
        measured=call('launcher','run',['--connect',addr,'--mode','udp','--config',cfg,'--key',key,'--plan',plan,'--output',session,'--window-ms','500','--duration','2'])
        saved=[p for p in session.iterdir() if p.is_file()]
        got=call('launcher','report',[session])
        d=json.loads(got.stdout)
        payload=d['directions']['forward']['phases']['whole']['received_bytes']
        record('launcher','run','--config/--key/--plan/--output/--connect','authenticated literal paths and key','Measure actual loopback UDP and preserve literal directory names',
               dict(exit=measured.returncode,received_bytes=payload,secret_absent=all(key.encode() not in p.read_bytes() for p in saved)),
               measured.returncode==0 and payload>0 and key not in (measured.stderr+measured.stdout) and all(key.encode() not in p.read_bytes() for p in saved))
        rejected=work/'unavailable-interface'
        result=call('launcher','run',['--connect',addr,'--mode','udp','--forward-max','320000','--interface',text,'--duration','1','--output',rejected])
        mf=json.loads((rejected/'session.json').read_text())
        record('launcher','run','--interface','unknown literal interface','Fail setup, retain failed manifest and zero admitted sessions',dict(exit=result.returncode,status=mf['status'],sessions=mf['sessions'],windows_file=(rejected/'windows.jsonl').exists()),result.returncode!=0 and mf['status']=='failed' and mf['sessions']==0 and not (rejected/'windows.jsonl').exists())
        missing=call('native','client',['--connect',addr,'--mode','udp','--forward-max','400000'],stdin='{\n')
        record('native','client','stdin','malformed window JSON','Fail after admission, zero receiver windows',
               dict(exit=missing.returncode,window_rows=missing.stdout.count('"window":'),secret_absent=key not in missing.stderr),
               missing.returncode!=0 and '"window":' not in missing.stdout and key not in missing.stderr)
        with (session/'events.csv').open('a',newline='') as f:
            csv.writer(f).writerow(['2026-01-01T00:00:00Z',1,'forward','operator_note',text+' <band A>'])
        html=work/(text+'.html')
        pub=call('launcher','report',[session,'--html',html])
        body=html.read_text()
        reader=PageText();reader.feed(body)
        record('launcher','report','--html','report encoding and literal destination','Neutral text round trips through HTML escaping',
               dict(exit=pub.returncode,escaped='&lt;band A&gt;' in body,text_roundtrip=text+' <band A>' in reader.words),
               pub.returncode==0 and '&lt;band A&gt;' in body and text+' <band A>' in reader.words and '<band A>' not in body)
        before=hashlib.sha256(html.read_bytes()).hexdigest()
        dup=call('launcher','report',[session,'--html',html])
        record('launcher','report','--html','existing file','Reject and preserve existing bytes',dict(exit=dup.returncode),dup.returncode!=0 and hashlib.sha256(html.read_bytes()).hexdigest()==before)
    finally:
        r.terminate();r.communicate(timeout=3)
    with socket.socket() as s:
        host,port=addr.rsplit(':',1)
        released=s.connect_ex((host,int(port)))!=0
    record('launcher','server','--listen','teardown','Listener released after stop',dict(released=released),released)
with (out/'checks.csv').open('w') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
(out/'summary.json').write_text(json.dumps(dict(package_sha256=digest,passed=sum(r['status']=='PASS' for r in rows),failed=[(r['program'],r['command'],r['surface'],r['case']) for r in rows if r['status']=='FAIL']),indent=2)+'\n')
print((out/'summary.json').read_text())
raise SystemExit(int(any(row['status']=='FAIL' for row in rows)))
