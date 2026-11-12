"""Real Codex binary + localhost fixture provider. No real model inference."""
import argparse, json, os, sys, threading, time, uuid, subprocess, shlex
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
parser=argparse.ArgumentParser()
parser.add_argument('--codex',required=True)
parser.add_argument('--plugin-repo',default=str(Path(__file__).resolve().parents[2]))
parser.add_argument('--out',required=True)
parser.add_argument('--workspace-write',action='store_true')
parser.add_argument('--extra-writable-root',action='store_true')
parser.add_argument('--official-process-wrappers',action='store_true',help='Exercise the shipped plugin isolation implementation')
parser.add_argument('--isolated-rewind-home',action='store_true')
parser.add_argument('--plugin-integrations',action='store_true')
parser.add_argument('--probe-hook-isolation',action='store_true',
                    help='Require a retained hook and its subprocess to be unable to write the original checkout')
args=parser.parse_args()
if args.probe_hook_isolation:
 args.official_process_wrappers=True;args.isolated_rewind_home=True
if args.plugin_integrations and not args.probe_hook_isolation:
 parser.error('--plugin-integrations requires --probe-hook-isolation')
sys.path.insert(0,str(Path(args.plugin_repo).resolve()))
from harness.drivers.codex_appserver import AppServer
from harness.drivers.codex import review_hooks
probe_root=Path(args.out).resolve();probe_root.mkdir(parents=True,exist_ok=False)
source=probe_root/'source';branch=probe_root/'branch';source.mkdir(exist_ok=True);branch.mkdir(exist_ok=True)
extra=probe_root/'artifacts';extra.mkdir()
if args.extra_writable_root and not args.workspace_write:
 parser.error('--extra-writable-root requires --workspace-write')
(source/'tracked.txt').write_text('original fixture\n');(branch/'tracked.txt').write_text('original fixture\n')
subprocess.run(['git','init','-q'],cwd=source,check=True)
subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@invalid','add','.'],cwd=source,check=True)
subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@invalid','commit','-qm','fixture'],cwd=source,check=True)
sys.path.insert(0,str(Path(args.plugin_repo)/'plugins/memhub/scripts'))
from rewind.codex import Codex
from rewind.checkpoints import Store
requests=[]
write_probe_sent=False
hook_log=probe_root/'hook-events.jsonl'
hook_log.write_text('')
hook_script=probe_root/'checkpoint_hook.py'
hook_script.write_text("import sys,json\nfrom pathlib import Path\np=json.load(sys.stdin)\nwith Path("+repr(str(hook_log))+").open('a') as f: f.write(json.dumps(p)+'\\n')\n")
hook_script.write_text(hook_script.read_text()+"\nsys.path.insert(0,"+repr(str(Path(args.plugin_repo)/'plugins/memhub/scripts'))+")\nfrom rewind.hooks import capture_prompt\nif p.get('hook_event_name') == 'UserPromptSubmit': capture_prompt(p)\n")
if args.probe_hook_isolation:
 hook_script.write_text(hook_script.read_text()+f'''
if Path(p['cwd']).resolve() == Path({str(branch)!r}):
    import subprocess
    attempts = []
    targets = [{str(source/'forbidden-hook-write')!r}, {str(source/'.git'/'forbidden-hook-write')!r}]
    transcript_file = Path({str(probe_root/'protected-transcript-path')!r})
    if transcript_file.exists(): targets.append(transcript_file.read_text())
    for target in targets:
        code = 'from pathlib import Path; Path(' + repr(target) + ').write_text("hook escaped")'
        child = subprocess.run([sys.executable, '-c', code], capture_output=True)
        attempts.append({{'target': target, 'denied': child.returncode != 0 and b'PermissionError' in child.stderr}})
    with Path({str(probe_root/'hook-isolation.jsonl')!r}).open('a') as f:
        f.write(json.dumps({{'event': p['hook_event_name'], 'session_id':p.get('session_id'), 'plugin_fixture':p.get('plugin_fixture',False), 'attempts': attempts}}) + '\\n')
''')
if args.probe_hook_isolation:
 process_probe=probe_root/'process_probe.py'
 process_probe.write_text(f'''import json, os, subprocess, sys
from pathlib import Path
with Path({str(probe_root/'process-launches.jsonl')!r}).open('a') as f: f.write(json.dumps(dict(mode=sys.argv[1],cwd=os.getcwd(),home=os.environ.get('CODEX_HOME')))+'\\n')

def probe(event, cwd):
    if Path(cwd).resolve() != Path({str(branch)!r}) and not (event == 'plugin_stdio' and Path(os.environ.get('CODEX_HOME','')).is_relative_to(Path({str(probe_root/'plugin-state/rewind/clients')!r}))): return
    attempts=[]
    targets = [{str(source/'forbidden-process-write')!r}, {str(source/'.git'/'forbidden-process-write')!r}]
    transcript_file = Path({str(probe_root/'protected-transcript-path')!r})
    if transcript_file.exists(): targets.append(transcript_file.read_text())
    for target in targets:
        code='from pathlib import Path; Path('+repr(target)+').write_text("escaped")'
        child=subprocess.run([sys.executable,'-c',code],capture_output=True)
        attempts.append({{"target":target,"denied":child.returncode != 0 and b"PermissionError" in child.stderr}})
    with Path({str(probe_root/'process-isolation.jsonl')!r}).open('a') as log:
        log.write(json.dumps({{"event":event,"attempts":attempts}})+"\\n")

if sys.argv[1]=='notify':
    probe('notify',json.loads(sys.argv[-1])['cwd'])
else:
    for line in sys.stdin:
        request=json.loads(line)
        if 'id' not in request: continue
        method=request['method']
        if method=='initialize':
            probe(sys.argv[1],os.getcwd())
            result={{"protocolVersion":request['params']['protocolVersion'],"capabilities":{{"tools":{{}}}},"serverInfo":{{"name":"source-guard-fixture","version":"1"}}}}
        elif method=='tools/list': result={{"tools":[]}}
        else: result={{}}
        print(json.dumps({{"jsonrpc":"2.0","id":request['id'],"result":result}}),flush=True)
''')
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  global write_probe_sent
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append({**body,'_hook_count':sum(json.loads(line).get('hook_event_name')=='UserPromptSubmit' for line in hook_log.read_text().splitlines())})
  rid='resp_'+uuid.uuid4().hex;mid='msg_'+uuid.uuid4().hex
  item={'id':mid,'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'fixture answer','annotations':[]}]}
  response={'id':rid,'object':'response','created_at':int(time.time()),'status':'completed','model':body.get('model'),'output':[item],'usage':{'input_tokens':10,'output_tokens':3,'total_tokens':13}}
  events=[('response.created',{'response':{**response,'status':'in_progress','output':[]}}),('response.output_item.added',{'output_index':0,'item':{**item,'status':'in_progress','content':[]}}),('response.output_text.delta',{'item_id':mid,'output_index':0,'content_index':0,'delta':'fixture answer'}),('response.output_item.done',{'output_index':0,'item':item}),('response.completed',{'response':response})]
  if args.workspace_write and body.get('model')=='fixture-b' and not write_probe_sent:
   write_probe_sent=True
   available=[t.get('name') for t in body.get('tools',[]) if t.get('type')=='function']
   assert 'exec_command' in available, available
   command='printf branch-write > '+shlex.quote(str(branch/'write-proof'))+'; if printf forbidden > '+shlex.quote(str(source/'forbidden-write'))+' 2>/dev/null; then exit 91; fi'
   if args.extra_writable_root:
    command += '; printf extra-write > '+shlex.quote(str(extra/'write-proof'))
   arguments=json.dumps({'cmd':command,'max_output_tokens':1000})
   item={'type':'function_call','id':'fc_'+uuid.uuid4().hex,'call_id':'call_'+uuid.uuid4().hex,'name':'exec_command','arguments':arguments,'status':'completed'}
   response['output']=[item]
   events=[('response.created',{'response':{**response,'status':'in_progress','output':[]}}),
           ('response.output_item.added',{'output_index':0,'item':{**item,'status':'in_progress','arguments':''}}),
           ('response.function_call_arguments.delta',{'item_id':item['id'],'output_index':0,'delta':arguments}),
           ('response.output_item.done',{'output_index':0,'item':item}),('response.completed',{'response':response})]
  raw=''.join('event: '+typ+'\ndata: '+json.dumps({'type':typ,**data})+'\n\n' for typ,data in events).encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
client_home=probe_root/'client';client_home.mkdir(exist_ok=True)
(client_home/'config.toml').write_text(f'''model_provider = "rewind_fixture"
model = "fixture-a"
[model_providers.rewind_fixture]
name = "Rewind local fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
''')
if args.probe_hook_isolation:
 config_text=(client_home/'config.toml').read_text().replace('model = "fixture-a"\n',
     'model = "fixture-a"\nnotify = '+json.dumps([sys.executable,str(process_probe),'notify'])+'\n')
 config_text+='\n[mcp_servers.source_guard_fixture]\ncommand = '+json.dumps(sys.executable)+'\nargs = '+json.dumps([str(process_probe),'stdio'])+'\n'
 (client_home/'config.toml').write_text(config_text)
handlers={'UserPromptSubmit':[{'hooks':[{'type':'command','command':sys.executable+' '+str(hook_script),'timeout':10}]}]}
if args.probe_hook_isolation:
 handlers['SessionStart']=[{'hooks':[{'type':'command','command':sys.executable+' '+str(hook_script),'timeout':10}]}]
 handlers['PostToolUse']=[{'hooks':[{'type':'command','command':sys.executable+' '+str(hook_script),'timeout':10,'async':True}]}]
(client_home/'hooks.json').write_text(json.dumps({'hooks':handlers}))
if args.plugin_integrations:
 plugin=client_home/'plugins/cache/fixture/probe/1.0.0'
 (plugin/'.codex-plugin').mkdir(parents=True)
 (plugin/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'probe','version':'1.0.0','hooks':'./hooks/hooks.json'}))
 (plugin/'hooks').mkdir()
 (plugin/'hook.py').write_text(hook_script.read_text().replace('p=json.load(sys.stdin)',"p=json.load(sys.stdin);p['plugin_fixture']=True"))
 (plugin/'hooks/hooks.json').write_text(json.dumps({'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':sys.executable+' "${PLUGIN_ROOT}/hook.py"','timeout':10}]}]}}))
 (plugin/'process.py').write_text(process_probe.read_text())
 (plugin/'.mcp.json').write_text(json.dumps({'mcpServers':{'plugin_fixture':{'command':sys.executable,'args':[str(plugin/'process.py'),'plugin_stdio']}}}))
 with (client_home/'config.toml').open('a') as f:f.write('\n[features]\nplugins = true\n[plugins."probe@fixture"]\nenabled = true\n')
os.environ['MEMHUB_CONFIG_DIR']=str(probe_root/'plugin-state')
from rewind.state import root,write,read,digest
write(root()/'settings.json',{'enabled':True})
env={**os.environ,'CODEX_HOME':str(client_home)}
rpc=AppServer([args.codex,'app-server'],cwd=source,env=env,log=probe_root/'stderr.log')
summary={'binary':args.codex,'provider':'local deterministic fixture; no real model inference'}
def turn(tid,text,image=False):
 result=rpc.request('turn/start',{'threadId':tid,'clientUserMessageId':str(uuid.uuid4()),'input':[{'type':'text','text':text}]+([{'type':'image','url':'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPioAAAAASUVORK5CYII='}] if image else [])},timeout_s=20)
 deadline=time.time()+25
 while time.time()<deadline:
  note=rpc.notes.get(timeout=25)
  if note and ('mcp' in note.get('method','').lower() or 'error' in note.get('method','').lower()):
   with (probe_root/'native-notices.jsonl').open('a') as f:f.write(json.dumps(note)+'\n')
  if note and note.get('method')=='turn/completed' and note.get('params',{}).get('threadId')==tid:
   done=note['params']['turn'];assert done['status']=='completed',done;return done['id']
 raise RuntimeError('turn did not complete')
try:
 rpc.request('initialize',{'clientInfo':{'name':'memhub-rewind-feasibility','version':'1'},'capabilities':{'experimentalApi':True}});rpc.notify('initialized')
 listed=rpc.request('hooks/list',{'cwds':[str(source)]})
 hooks=[h for entry in listed.get('data',[]) for h in entry.get('hooks',[])]
 assert len(hooks)==len(handlers)+int(args.plugin_integrations) and all(str(client_home) in hook['sourcePath'] for hook in hooks), ('Unexpected hook in disposable configuration',[(h['eventName'],h['sourcePath']) for h in hooks])
 reviewed_hooks=review_hooks(rpc,source)
 start=rpc.request('thread/start',{'cwd':str(source),'model':'fixture-a','approvalPolicy':'never',
    'sandbox':'workspace-write' if args.workspace_write else 'read-only',
    'config':{'sandbox_workspace_write':{'exclude_tmpdir_env_var':True,'exclude_slash_tmp':True,
      'writable_roots':[str(extra)] if args.extra_writable_root else []}}})
 tid=start['thread']['id'];t1=turn(tid,'SOURCE ONE',image=True)
 (source/'tracked.txt').write_text('manual between turns\n')
 t2=turn(tid,'SOURCE TWO')
 (source/'tracked.txt').write_text('later source state\n')
 t3=turn(tid,'SOURCE THREE')
 hook_rows=[row for line in hook_log.read_text().splitlines() if (row:=json.loads(line)).get('hook_event_name')=='UserPromptSubmit']
 assert [h['turn_id'] for h in hook_rows]==[t1,t2,t3]
 summary['hook_turn_ids_match_native_turns']=True
 if not args.official_process_wrappers:
  empty=rpc.request('thread/fork',{'threadId':tid,'beforeTurnId':t1,'cwd':str(branch),'excludeTurns':False})
  assert len(rpc.request('thread/read',{'threadId':empty['thread']['id'],'includeTurns':True})['thread']['turns'])==0
 if not args.isolated_rewind_home: before=rpc.request('thread/read',{'threadId':tid,'includeTurns':True})
 adapter=Codex(args.codex,rpc=rpc)
 boundary=read(root()/'boundaries'/(digest([tid,t2])+'.json'))
 from rewind.checkpoints import find_checkpoint
 checkpoint=find_checkpoint(boundary['checkpoint_id'])
 branch.rmdir() if not list(branch.iterdir()) else None
 (branch/'tracked.txt').unlink();branch.rmdir()
 Store(source).restore(checkpoint,branch)
 assert (branch/'tracked.txt').read_text()=='manual between turns\n'
 from rewind.hooks import context_for
 config={'model_id':'fixture-b','_context':context_for(checkpoint['local_config']['transcript_path'],t2)}
 if args.probe_hook_isolation:
  (probe_root/'protected-transcript-path').write_text(checkpoint['local_config']['transcript_path'])
 from rewind.codex_safety import validate_context
 validate_context(config['_context'],str(source))
 if args.official_process_wrappers:
  from rewind.isolation import inspect
  from unittest.mock import patch
  import hashlib
  summary['official_binary_sha256']=hashlib.sha256(Path(args.codex).read_bytes()).hexdigest()
  summary['official_binary_version']=subprocess.check_output([args.codex,'--version'],text=True).strip()
  original_client_home=client_home
  original_config=(client_home/'config.toml').read_bytes()
  original_hooks=(client_home/'hooks.json').read_bytes()
  original_transcript=Path(checkpoint['local_config']['transcript_path'])
  rpc.close()
  original_transcript_bytes=original_transcript.read_bytes()
  rpc=AppServer([args.codex,'app-server'],cwd=source,env=env,log=probe_root/'inspect-stderr.log')
  rpc.request('initialize',{'clientInfo':{'name':'memhub-rewind-fixture','version':'1'},'capabilities':{'experimentalApi':True}});rpc.notify('initialized')
  adapter.rpc=rpc
  with patch.dict(os.environ,{'CODEX_HOME':str(client_home)}):
   config['_local_tools']=inspect(rpc,source)
  # All hooks here are authored by this fixture in its disposable home.
  with patch.object(sys.stdin,'isatty',return_value=True), patch('builtins.input',return_value='y'):
   adapter.prepare(checkpoint,config,str(branch))
  rpc=adapter.rpc
  client_home=Path(config['_isolation']['home'])
  env={**env,'CODEX_HOME':str(client_home)}
  reviewed_hooks=[{'event':h['eventName'],'trust':h['trustStatus']} for row in rpc.request('hooks/list',{'cwds':[str(branch)]})['data'] for h in row['hooks']]
  assert all(h['trust']=='trusted' for h in reviewed_hooks)
  summary['plugin_process_isolation']=True
  empty=rpc.request('thread/fork',{'threadId':tid,'beforeTurnId':t1,'cwd':str(branch),'excludeTurns':False})
  assert adapter.turns(empty['thread']['id'])==[]
 native_request=rpc.request
 def recorded_request(method,params,**kwargs):
  result=native_request(method,params,**kwargs)
  if method=='thread/fork':summary['fork_sandbox_policy']=result.get('sandbox')
  return result
 rpc.request=recorded_request
 native=adapter.fork(checkpoint,config,str(branch))
 fork={'thread':{'id':native['native_branch_id']},'model':'fixture-b'}
 child=fork['thread']['id'];assert child!=tid
 fork_turns=rpc.request('thread/read',{'threadId':child,'includeTurns':True})['thread']['turns']
 assert len(fork_turns)==1,(len(fork_turns),fork_turns)
 assert fork_turns[0]['id']==t1
 assert 'data:image/png' in json.dumps(fork_turns)
 assert [r['_hook_count'] for r in requests[:3]]==[1,2,3],[(r['_hook_count']) for r in requests]
 summary.update({'synchronous_prompt_hook':True,'hook_payload_fields':sorted(json.loads(hook_log.read_text().splitlines()[0])),'image_prefix_preserved':True,'before_first_turn_empty':True,'reviewed_hooks':reviewed_hooks})
 turn(child,'REPLACEMENT TWO');turn(child,'FOLLOWUP THREE')
 if args.workspace_write:
  assert (branch/'write-proof').read_text()=='branch-write'
  assert not (source/'forbidden-write').exists(), 'native sandbox allowed a source write'
  summary['branch_write_allowed_source_write_denied']=True
  if args.extra_writable_root:
   assert (extra/'write-proof').read_text()=='extra-write'
   summary['extra_writable_root_preserved']=True
 if args.probe_hook_isolation:
  # A new native process receives no hook restriction config: persistence must
  # restore it before SessionStart and subsequent prompt/async hooks execute.
  pre_restart_rows=len((probe_root/'hook-isolation.jsonl').read_text().splitlines())
  rpc.close()
  if args.official_process_wrappers:
   rpc=AppServer([args.codex,'app-server'],cwd=source,env={**env,'CODEX_HOME':str(original_client_home)},log=probe_root/'resume-stderr.log')
   rpc.request('initialize',{'clientInfo':{'name':'memhub-rewind-feasibility','version':'1'},'capabilities':{'experimentalApi':True}});rpc.notify('initialized')
   adapter.rpc=rpc
   adapter.prepare(checkpoint,config,str(branch)) # Existing review reused; no mocked approval.
   rpc=adapter.rpc
  else:
   rpc=AppServer([args.codex,'app-server'],cwd=branch,env=env,log=probe_root/'resume-stderr.log')
   rpc.request('initialize',{'clientInfo':{'name':'memhub-rewind-feasibility','version':'1'},'capabilities':{'experimentalApi':True}});rpc.notify('initialized')
 resumed=rpc.request('thread/resume',{'threadId':child,'cwd':str(branch),'excludeTurns':False})
 if args.probe_hook_isolation:
  turn(child,'AFTER PROCESS RESTART')
  refork=rpc.request('thread/fork',{'threadId':child,'beforeTurnId':t1,'cwd':str(branch),
                                  'deferGoalContinuation':True,'excludeTurns':False})
  turn(refork['thread']['id'],'REFORK VALIDATION')
  deadline=time.time()+10
  while True:
   rows=[json.loads(line) for line in (probe_root/'hook-isolation.jsonl').read_text().splitlines()]
   if any(row.get('session_id')==refork['thread']['id'] for row in rows): break
   if time.time()>deadline: raise AssertionError('Refork hook never ran')
   time.sleep(.05)
  attempts=[item for row in rows for item in row['attempts']]
  summary['hook_source_writes_denied']=bool(attempts) and all(item['denied'] for item in attempts)
  assert summary['hook_source_writes_denied'], 'Native hook subprocess wrote the original checkout or Git directory'
  assert {'SessionStart','UserPromptSubmit'} <= {row['event'] for row in rows[pre_restart_rows:]}, 'Hooks did not run after process restart'
  if args.workspace_write:
   assert any(row['event']=='PostToolUse' for row in rows), 'Async tool hook did not execute'
  summary['hook_protection_survived_process_restart']=True
  summary['hook_protection_survived_refork']=True
  if args.plugin_integrations:
   assert any(row.get('plugin_fixture') for row in rows), 'Plugin hook did not execute'
   summary['plugin_hook_protected']=True
  deadline=time.time()+10
  while True:
   process_rows=[json.loads(line) for line in (probe_root/'process-isolation.jsonl').read_text().splitlines()] if (probe_root/'process-isolation.jsonl').exists() else []
   required={'stdio','notify'} | ({'plugin_stdio'} if args.plugin_integrations else set())
   if required <= {row['event'] for row in process_rows}: break
   if time.time()>deadline: raise AssertionError('Protected stdio and notification processes did not both run')
   time.sleep(.05)
  assert all(item['denied'] for row in process_rows for item in row['attempts']), 'Local subprocess wrote source or Git storage'
  summary['stdio_and_notify_source_writes_denied']=True
  summary['plugin_stdio_protected']=args.plugin_integrations
 assert resumed['thread']['id']==child
 if args.isolated_rewind_home:
  assert (original_client_home/'config.toml').read_bytes()==original_config
  assert (original_client_home/'hooks.json').read_bytes()==original_hooks
  assert original_transcript.read_bytes()==original_transcript_bytes
  summary['separate_rewind_home_original_config_and_transcript_unchanged']=True
 else:
  after=rpc.request('thread/read',{'threadId':tid,'includeTurns':True})
  assert before['thread']['turns']==after['thread']['turns']
 assert (source/'tracked.txt').read_text()=='later source state\n'
 assert any(r.get('model')=='fixture-b' for r in requests)
 from rewind.capture import prepare
 from rewind.runner import register_branch
 from rewind.state import branch_path
 from rewind.hooks import metadata
 source_path=Path(checkpoint['local_config']['transcript_path'])
 source_records,_,_=prepare(source_path,tid)
 selected=[r for r in source_records if r.get('rewind_v1',{}).get('checkpoint',{}).get('native_turn_id')==t2]
 assert len(selected)==1, 'exact native turn checkpoint absent from canonical capture'
 selected_event=selected[0]['uuid']
 journal={'comparison_id':str(uuid.uuid4()),'run_id':str(uuid.uuid4()),'claim_id':str(uuid.uuid4()),
          'plan':{'source':{'workspace_id':str(uuid.uuid4()),'conv_id':str(uuid.uuid4())},
                  'boundary':{'selected_event':{'event_id':selected_event,'capture_version':1}}}}
 register_branch(journal,checkpoint,native)
 child_path=next(client_home.joinpath('sessions').rglob('*'+child+'.jsonl'))
 child_records,_,lineage=prepare(child_path,child)
 assert lineage['native_branch_id']==child and lineage['prefix_mapping']==[]
 assert lineage['continuation_start_event_id']==child_records[0]['uuid']
 assert all(r['native_event_id']==r['uuid'] for r in child_records)
 summary['capture_checkpoint_and_lineage']=True
 if args.official_process_wrappers:
  assert hashlib.sha256(Path(args.codex).read_bytes()).hexdigest()==summary['official_binary_sha256']
  summary['official_binary_unchanged']=True
 summary.update({'pass':True,'original_turns':3,'fork_prefix_turns':len(fork_turns),'continued_turns':3 if args.probe_hook_isolation else 2,'source_preserved':True,'model_requests':[r.get('model') for r in requests],'source_id':tid,'branch_id':child,'fork_response_model':fork.get('model'),'checkpoint_before_selected_turn':True,'workspace_write':args.workspace_write,'branch_cwd':resumed.get('cwd'),'source_root_id':start['thread'].get('sessionId'),'fork_root_id':fork['thread'].get('sessionId')})
except Exception as e:
 summary.update({'pass':False,'error':str(e),'requests_seen':len(requests)});raise
finally:
 rpc.close();server.shutdown();(probe_root/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary))
