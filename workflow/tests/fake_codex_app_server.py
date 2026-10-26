"""Deterministic fake for Codex app-server transport tests. No model/network."""
import json
import os
import sys
from pathlib import Path

# Parent bridge speaks UTF-8 JSONL; Windows console defaults can differ.
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')

storage = Path(os.environ['FAKE_CODEX_STATE'])
state = json.loads(storage.read_text(encoding='utf-8')) if storage.exists() else {"threads": {}, "history": [], "counter": 0}

def save():
    storage.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + '\n')
    sys.stdout.flush()

def result_for(schema):
    props = schema.get('properties', {})
    if 'questions' in props:
        return {"summary":"Создать уведомления по утверждённой идее", "questions":[], "in_scope":["Уведомления"],
                "out_of_scope":[], "acceptance":["Сценарии согласованы"], "assumptions":[]}
    if 'modules' in props:
        return {"modules":[{"id":"evals", "template":"evals", "inputs":[], "goal":"Критерии проверки"},
                           {"id":"documentation", "template":"documentation", "inputs":["evals"],
                            "goal":"Аудит документации"}],
                "rationale":"Minimal ordered independent modules", "risks":[], "system_design_mode":"none"}
    if 'decisions_for_user' in props:
        return {"summary":"Работы по шагу зафиксированы", "decisions_for_user":[], "risks":[],
                "next_step":"Только после утверждения", "mermaid":"flowchart TB\n A[Done] --> B[Approval]"}
    if 'instructions' in props:
        return {"instructions":"Work within scope", "risks":[]}
    base = {"status":"completed", "summary":"Агент подготовил результат",
            "evidence":["Эмулятор app-server: не реальная проверка"],
            "mermaid":"flowchart TB\n A[Result] --> B[Approval]",
            "decisions":[], "risks":[], "docs_handoff":"No changes detected",
            "changed_files":[], "no_changes_needed":True}
    if 'system_design' in props:
        base['system_design'] = {name: 'Specific design section with enough characters'
                                  for name in props['system_design']['properties']}
    return base

for raw in sys.stdin:
    try:
        req=json.loads(raw)
        method=req.get('method')
        params=req.get('params') or {}
        if method=='initialize':
            send({'id':req['id'],'result':{'server':'fake'}})
        elif method=='initialized':
            continue
        elif method=='thread/start':
            state['counter']+=1
            ident=f"fake-thread-{state['counter']}"
            state['threads'][ident]={"name":"", "turns":0}
            state['history'].append({"method":"thread/start", "threadId":ident})
            save()
            send({'id':req['id'],'result':{'thread':{'id':ident}}})
        elif method=='thread/name/set':
            ident=params['threadId']
            state['threads'][ident]['name']=params['name']
            state['history'].append({"method":method,"threadId":ident,"name":params['name']})
            save()
            send({'id':req['id'],'result':{}})
        elif method=='thread/resume':
            ident=params['threadId']
            if ident not in state['threads']:
                send({'id':req['id'],'error':{'code':-32602,'message':'missing'}})
            else:
                state['history'].append({"method":method,"threadId":ident})
                save()
                send({'id':req['id'],'result':{'thread':{'id':ident}}})
        elif method=='turn/start':
            ident=params['threadId']
            state['threads'][ident]['turns']+=1
            turn=f"fake-turn-{ident}-{state['threads'][ident]['turns']}"
            state['history'].append({"method":method,"threadId":ident,"sandbox":params.get('sandboxPolicy'),
                                     "prompt":params.get('input',[{}])[0].get('text','')[:100]})
            save()
            send({'id':req['id'],'result':{'turn':{'id':turn,'status':'inProgress'}}})
            if os.environ.get('FAKE_CODEX_DISCONNECT')=='1':
                sys.exit(0)
            if os.environ.get('FAKE_CODEX_REQUEST_APPROVAL')=='1':
                send({'id':'request-1','method':'item/commandExecution/requestApproval',
                      'params':{'threadId':ident,'turnId':turn,'command':['touch','forbidden']}})
                approval=json.loads(sys.stdin.readline())
                state.setdefault('approval_responses',[]).append(approval)
                save()
            if 'commandExecution' not in os.environ.get('FAKE_CODEX_NO_COMMAND',''):
                send({'method':'item/completed','params':{'threadId':ident,'turnId':turn,
                      'item':{'type':'commandExecution','status':'completed','exitCode':0}}})
            output=result_for(params['outputSchema'])
            send({'method':'item/completed','params':{'threadId':ident,'turnId':turn,
                  'item':{'type':'agentMessage','text':json.dumps(output, ensure_ascii=False)}}})
            send({'method':'turn/completed','params':{'threadId':ident,'turn':{'id':turn,'status':'completed'}}})
        else:
            if 'id' in req:
                send({'id':req['id'],'error':{'code':-32601,'message':method or ''}})
    except Exception as exc:
        print('FAKE SERVER ERROR:',exc,file=sys.stderr)
        raise
